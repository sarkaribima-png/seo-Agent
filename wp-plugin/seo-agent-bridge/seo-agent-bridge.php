<?php
/**
 * Plugin Name: SEO Agent Bridge
 * Description: Secure REST endpoints for the SEO agent: page audit, AIOSEO title/meta with backup, and reversible schema / section / internal-link / image alt text changes rendered at display time (page content is never overwritten).
 * Version: 0.4.0
 * Requires PHP: 7.4
 */
if (!defined('ABSPATH')) exit;

final class SAB {
    const NS = 'seo-agent/v1';
    const BACKUP_META = '_sab_backups';
    const MODS_META = '_sab_mods';

    static function init() {
        add_action('rest_api_init', [__CLASS__, 'routes']);
        add_action('wp_head', [__CLASS__, 'print_schema'], 99);
        add_filter('the_content', [__CLASS__, 'render_content'], 999);
    }

    static function routes() {
        $can_read  = function () { return current_user_can('edit_pages'); };
        $can_write = function (WP_REST_Request $r) { $id = (int)$r['post_id']; return $id && current_user_can('edit_post', $id); };
        register_rest_route(self::NS, '/ping', ['methods' => 'GET', 'permission_callback' => $can_read,
            'callback' => function () { return ['ok' => true, 'version' => '0.4.0', 'mods' => true, 'alt' => true, 'after_heading' => true, 'aioseo' => self::aioseo_ready()]; }]);
        register_rest_route(self::NS, '/audit/(?P<post_id>\d+)', ['methods' => 'GET', 'permission_callback' => $can_read, 'callback' => [__CLASS__, 'audit']]);
        register_rest_route(self::NS, '/missing-meta', ['methods' => 'GET', 'permission_callback' => $can_read, 'callback' => [__CLASS__, 'missing_meta']]);
        register_rest_route(self::NS, '/seo/(?P<post_id>\d+)', ['methods' => 'POST', 'permission_callback' => $can_write, 'callback' => [__CLASS__, 'set_seo']]);
        register_rest_route(self::NS, '/mod/(?P<post_id>\d+)', ['methods' => 'POST', 'permission_callback' => $can_write, 'callback' => [__CLASS__, 'add_mod']]);
        register_rest_route(self::NS, '/mod-rollback/(?P<post_id>\d+)', ['methods' => 'POST', 'permission_callback' => $can_write, 'callback' => [__CLASS__, 'rollback_mod']]);
        register_rest_route(self::NS, '/mods/(?P<post_id>\d+)', ['methods' => 'GET', 'permission_callback' => $can_read, 'callback' => function (WP_REST_Request $r) { return ['mods' => self::mods((int)$r['post_id'])]; }]);
        register_rest_route(self::NS, '/rollback/(?P<post_id>\d+)', ['methods' => 'POST', 'permission_callback' => $can_write, 'callback' => [__CLASS__, 'rollback']]);
    }

    static function aioseo_ready() { global $wpdb; return (bool)$wpdb->get_var($wpdb->prepare('SHOW TABLES LIKE %s', $wpdb->prefix . 'aioseo_posts')); }
    static function table() { global $wpdb; return $wpdb->prefix . 'aioseo_posts'; }

    static function get_seo($id) {
        global $wpdb; if (!self::aioseo_ready()) return ['title' => '', 'description' => ''];
        $r = $wpdb->get_row($wpdb->prepare('SELECT title, description FROM ' . self::table() . ' WHERE post_id=%d', $id), ARRAY_A);
        return ['title' => (string)($r['title'] ?? ''), 'description' => (string)($r['description'] ?? '')];
    }

    static function write_seo($id, $title, $desc) {
        global $wpdb; $t = self::table();
        $exists = $wpdb->get_var($wpdb->prepare("SELECT id FROM $t WHERE post_id=%d", $id));
        $now = current_time('mysql', true);
        if ($exists) $ok = $wpdb->update($t, ['title' => $title, 'description' => $desc, 'updated' => $now], ['post_id' => $id]);
        else $ok = $wpdb->insert($t, ['post_id' => $id, 'title' => $title, 'description' => $desc, 'created' => $now, 'updated' => $now]);
        if ($ok === false) return new WP_Error('sab_db', $wpdb->last_error, ['status' => 500]);
        clean_post_cache($id); do_action('litespeed_purge_post', $id);
        return true;
    }

    static function set_seo(WP_REST_Request $r) {
        $id = (int)$r['post_id']; $p = $r->get_json_params();
        $title = isset($p['title']) ? sanitize_text_field($p['title']) : null;
        $desc  = isset($p['description']) ? sanitize_text_field($p['description']) : null;
        if ($title === null || $desc === null) return new WP_Error('sab_args', 'title and description required', ['status' => 400]);
        if (mb_strlen($title) > 70 || mb_strlen($desc) > 170) return new WP_Error('sab_len', 'too long', ['status' => 400]);
        $old = self::get_seo($id);
        $b = get_post_meta($id, self::BACKUP_META, true); if (!is_array($b)) $b = [];
        $bid = count($b) + 1; $b[] = ['id' => $bid, 'ts' => time(), 'user' => get_current_user_id(), 'old' => $old, 'new' => ['title' => $title, 'description' => $desc]];
        update_post_meta($id, self::BACKUP_META, array_slice($b, -50));
        $w = self::write_seo($id, $title, $desc); if (is_wp_error($w)) return $w;
        return ['ok' => true, 'backup_id' => $bid, 'old' => $old, 'new' => self::get_seo($id)];
    }

    static function rollback(WP_REST_Request $r) {
        $id = (int)$r['post_id']; $bid = (int)($r->get_json_params()['backup_id'] ?? 0);
        $b = get_post_meta($id, self::BACKUP_META, true); if (!is_array($b)) $b = [];
        foreach ($b as $row) if ($row['id'] === $bid) {
            $w = self::write_seo($id, $row['old']['title'], $row['old']['description']); if (is_wp_error($w)) return $w;
            return ['ok' => true, 'restored' => $row['old']];
        }
        return new WP_Error('sab_nf', 'backup not found', ['status' => 404]);
    }


    /* ---------- v0.3: reversible additions, rendered at display time ---------- */
    static function mods($id) { $m = get_post_meta($id, self::MODS_META, true); return is_array($m) ? $m : []; }
    static function active($id, $kind) { return array_values(array_filter(self::mods($id), function ($m) use ($kind) { return !empty($m['active']) && $m['kind'] === $kind; })); }
    static function purge($id) { clean_post_cache($id); do_action('litespeed_purge_post', $id); if (function_exists('rocket_clean_post')) rocket_clean_post($id); }

    static function add_mod(WP_REST_Request $r) {
        $id = (int)$r['post_id']; $p = $r->get_json_params(); $kind = $p['kind'] ?? '';
        if (!get_post($id)) return new WP_Error('sab_nf', 'post not found', ['status' => 404]);
        $data = self::clean_mod($kind, $p);
        if (is_wp_error($data)) return $data;
        $all = self::mods($id); $next = 1; foreach ($all as $m) $next = max($next, (int)$m['id'] + 1);
        $all[] = ['id' => $next, 'kind' => $kind, 'data' => $data, 'ts' => time(), 'user' => get_current_user_id(), 'active' => true];
        update_post_meta($id, self::MODS_META, array_slice($all, -200)); self::purge($id);
        return ['ok' => true, 'mod_id' => $next, 'backup_id' => $next];
    }

    static function clean_mod($kind, $p) {
        if ($kind === 'schema') {
            $ld = $p['json_ld'] ?? null; if (!is_array($ld) || empty($ld['@type'])) return new WP_Error('sab_args', 'json_ld with @type required', ['status' => 400]);
            if (empty($ld['@context'])) $ld = ['@context' => 'https://schema.org'] + $ld;
            $j = wp_json_encode($ld); if (strlen($j) > 20000) return new WP_Error('sab_len', 'schema too large', ['status' => 400]);
            if (preg_match('/aggregaterating|ratingvalue|reviewcount|"@type"\s*:\s*"review"/i', $j)) return new WP_Error('sab_rule', 'rating/review schema is not allowed', ['status' => 400]);
            return ['json_ld' => $ld];
        }
        if ($kind === 'section') {
            $html = trim((string)($p['html'] ?? '')); if ($html === '' || strlen($html) > 12000) return new WP_Error('sab_args', 'section html required (max 12000 chars)', ['status' => 400]);
            $html = wp_kses_post($html);
            if (preg_match('/guarantee|100%|painless|scarless/i', wp_strip_all_tags($html))) return new WP_Error('sab_rule', 'risky claim in section', ['status' => 400]);
            $pos = self::clean_position($p['position'] ?? null); if (is_wp_error($pos)) return $pos;
            return ['html' => $html, 'position' => $pos];
        }
        if ($kind === 'link') {
            $a = trim(wp_strip_all_tags((string)($p['anchor'] ?? ''))); $u = esc_url_raw((string)($p['url'] ?? ''));
            if (mb_strlen($a) < 2 || mb_strlen($a) > 80 || !$u) return new WP_Error('sab_args', 'anchor and url required', ['status' => 400]);
            $h1 = strtolower(preg_replace('/^www\./', '', (string)parse_url(home_url(), PHP_URL_HOST))); $h2 = strtolower(preg_replace('/^www\./', '', (string)parse_url($u, PHP_URL_HOST)));
            if ($h1 !== $h2) return new WP_Error('sab_rule', 'links must stay on this site', ['status' => 400]);
            return ['anchor' => $a, 'url' => $u];
        }
        if ($kind === 'alt') {
            $items = $p['items'] ?? null;
            if (!is_array($items) || !$items || count($items) > 30) return new WP_Error('sab_args', 'items required (1 to 30 images)', ['status' => 400]);
            $out = [];
            foreach ($items as $it) {
                $src = esc_url_raw((string)($it['src'] ?? '')); $alt = trim(sanitize_text_field((string)($it['alt'] ?? '')));
                if (self::img_key($src) === '' || mb_strlen($alt) < 4 || mb_strlen($alt) > 125) return new WP_Error('sab_args', 'each item needs an image src and an alt of 4 to 125 characters', ['status' => 400]);
                if (preg_match('/guarantee|100%|painless|scarless|best (doctor|surgeon|clinic)|before and after|\.(jpe?g|png|webp|gif)\b/i', $alt)) return new WP_Error('sab_rule', 'risky claim or file name in alt text', ['status' => 400]);
                $out[] = ['src' => $src, 'alt' => $alt];
            }
            return ['items' => $out];
        }
        return new WP_Error('sab_args', 'kind must be schema, section, link or alt', ['status' => 400]);
    }

    /** position: 'before_faq' | 'after_faq' | 'end' | ['after_heading' => 'Heading text', 'occurrence' => 1] */
    static function clean_position($pos) {
        if (is_array($pos)) {
            $h = trim(wp_strip_all_tags((string)($pos['after_heading'] ?? '')));
            if ($h === '' || mb_strlen($h) > 200) return new WP_Error('sab_args', 'after_heading needs the heading text (max 200 chars)', ['status' => 400]);
            $o = ['after_heading' => $h]; $occ = max(1, min(20, (int)($pos['occurrence'] ?? 1))); if ($occ > 1) $o['occurrence'] = $occ;
            return $o;
        }
        return in_array($pos, ['before_faq', 'after_faq', 'end'], true) ? $pos : 'before_faq';
    }

    static function rollback_mod(WP_REST_Request $r) {
        $id = (int)$r['post_id']; $mid = (int)($r->get_json_params()['mod_id'] ?? $r->get_json_params()['backup_id'] ?? 0);
        $all = self::mods($id); $hit = false;
        foreach ($all as &$m) if ((int)$m['id'] === $mid) { $m['active'] = false; $m['off'] = time(); $hit = true; }
        unset($m);
        if (!$hit) return new WP_Error('sab_nf', 'change not found', ['status' => 404]);
        update_post_meta($id, self::MODS_META, $all); self::purge($id);
        return ['ok' => true, 'removed' => $mid];
    }

    static function print_schema() {
        if (is_admin() || !is_singular()) return; $id = get_queried_object_id(); if (!$id) return;
        foreach (self::active($id, 'schema') as $m)
            echo "\n<script type=\"application/ld+json\" data-sab=\"" . (int)$m['id'] . "\">" . wp_json_encode($m['data']['json_ld'], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_HEX_TAG) . "</script>\n";
    }

    static function render_content($content) {
        if (is_admin() || !is_singular() || (defined('REST_REQUEST') && REST_REQUEST) || doing_filter('get_the_excerpt') || doing_action('wp_head')) return $content;
        $id = get_queried_object_id(); if (!$id || (int)get_the_ID() !== (int)$id) return $content;
        foreach (self::active($id, 'section') as $m) if (strpos($content, 'data-sab="' . (int)$m['id'] . '"') === false) $content = self::inject_section($content, $m['data']['html'], (int)$m['id'], $m['data']['position'] ?? 'before_faq');
        foreach (self::active($id, 'link') as $m) if (strpos($content, 'data-sab="' . (int)$m['id'] . '"') === false) $content = self::link_first($content, $m['data']['anchor'], $m['data']['url'], (int)$m['id']);
        foreach (self::active($id, 'alt') as $m) if (strpos($content, 'data-sab="' . (int)$m['id'] . '"') === false) $content = self::alt_apply($content, $m['data']['items'], (int)$m['id']);
        return $content;
    }

    /** Put the new section before the FAQ, after the FAQ section, or at the end. Pure function. */
    static function inject_section($html, $section, $mid, $pos = 'before_faq') {
        $block = '<section class="sab-section" data-sab="' . (int)$mid . '">' . $section . '</section>';
        if (is_array($pos)) return self::inject_after_heading($html, $block, $pos);
        if ($pos === 'end') return $html . $block;
        if (!preg_match('/<h([1-6])[^>]*>(?:(?!<\/h[1-6]>).){0,200}?(frequently asked|faq)/is', $html, $m, PREG_OFFSET_CAPTURE)) return $html . $block;
        $start = $m[0][1];
        if ($pos === 'after_faq') {
            // the FAQ section ends where the next heading of the same or higher level starts (the questions inside are usually lower level)
            $lvl = (int)$m[1][0]; $from = $start + strlen($m[0][0]);
            if (preg_match('/<h([1-' . $lvl . '])[\s>]/i', $html, $n, PREG_OFFSET_CAPTURE, $from)) return substr($html, 0, $n[0][1]) . $block . substr($html, $n[0][1]);
            return $html . $block;
        }
        return substr($html, 0, $start) . $block . substr($html, $start);
    }

    static function norm_text($s) {
        $s = html_entity_decode(wp_strip_all_tags((string)$s), ENT_QUOTES | ENT_HTML5, 'UTF-8');
        return mb_strtolower(trim(preg_replace('/\s+/u', ' ', str_replace("\xC2\xA0", ' ', $s))));
    }

    /** Move $pos back out of up to $levels wrapper tags (div / section) that open right before it, so the block sits beside the old content, not inside a widget. */
    static function step_out($html, $pos, $levels = 3) {
        for ($i = 0; $i < $levels; $i++) {
            $from = max(0, $pos - 400); $tail = substr($html, $from, $pos - $from);
            if (preg_match('/<(div|section)\b[^>]*>\s*$/i', $tail, $w, PREG_OFFSET_CAPTURE)) $pos = $from + $w[0][1]; else break;
        }
        return $pos;
    }

    /** Put the block after the chosen heading's whole section (before the next heading of the same or a higher level).
     *  Exact heading text first; otherwise a heading that contains the text, but only if exactly one does. If the heading is not found the content is returned
     *  unchanged, so the agent's live check fails and it rolls the change back with a clear message. Pure function. */
    static function inject_after_heading($html, $block, $pos) {
        if (!preg_match_all('/<h([1-6])\b[^>]*>(.*?)<\/h\1>/is', $html, $all, PREG_SET_ORDER | PREG_OFFSET_CAPTURE)) return $html;
        $want = self::norm_text($pos['after_heading']); $occ = max(1, (int)($pos['occurrence'] ?? 1)); $exact = []; $part = [];
        foreach ($all as $m) { $t = self::norm_text($m[2][0]); if ($t === $want) $exact[] = $m; elseif ($want !== '' && mb_strpos($t, $want) !== false) $part[] = $m; }
        if ($exact) $hit = $exact[$occ - 1] ?? null; else $hit = (count($part) === 1 && $occ === 1) ? $part[0] : null;
        if (!$hit) return $html;
        $lvl = (int)$hit[1][0]; $from = $hit[0][1] + strlen($hit[0][0]); $at = strlen($html);
        if (preg_match('/<h([1-' . $lvl . '])[\s>]/i', $html, $n, PREG_OFFSET_CAPTURE, $from)) $at = self::step_out($html, $n[0][1], 3);
        return substr($html, 0, $at) . $block . substr($html, $at);
    }

    /** Stable key for an image file: file name without folders, query, size suffix (-300x200) and extension. */
    static function img_key($src) {
        $f = strtolower(basename((string)parse_url(html_entity_decode((string)$src), PHP_URL_PATH)));
        return preg_replace('/-\d{2,4}x\d{2,4}$/', '', preg_replace('/\.[a-z0-9]{2,5}$/', '', $f));
    }

    /** Fill EMPTY alt attributes of <img> tags whose file matches an item. Never overwrites an alt that already has text. Skips script, style and comments. Pure function. */
    static function alt_apply($html, $items, $mid) {
        $want = [];
        foreach ((array)$items as $it) { $k = self::img_key($it['src'] ?? ''); $a = trim(preg_replace('/\s+/', ' ', (string)($it['alt'] ?? ''))); if ($k !== '' && $a !== '' && !isset($want[$k])) $want[$k] = $a; }
        if (!$want) return $html;
        $parts = preg_split('#(<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->)#is', $html, -1, PREG_SPLIT_DELIM_CAPTURE);
        foreach ($parts as $i => $part) {
            if ($i % 2 === 1) continue;
            $parts[$i] = preg_replace_callback('/<img\b[^>]*>/i', function ($m) use ($want, $mid) {
                $tag = $m[0];
                if (preg_match('/\salt\s*=\s*(?:"([^"]*)"|\'([^\']*)\')/i', $tag, $a) && trim($a[1] . ($a[2] ?? '')) !== '') return $tag;
                $keys = [];
                foreach (['src', 'data-src', 'data-lazy-src', 'data-original'] as $at) if (preg_match('/\s' . $at . '\s*=\s*(?:"([^"]*)"|\'([^\']*)\')/i', $tag, $v)) $keys[] = self::img_key($v[1] !== '' ? $v[1] : ($v[2] ?? ''));
                foreach (['srcset', 'data-srcset'] as $at) if (preg_match('/\s' . $at . '\s*=\s*"([^"]*)"/i', $tag, $v)) foreach (explode(',', $v[1]) as $c) { $u = preg_split('/\s+/', trim($c)); if ($u && $u[0] !== '') $keys[] = self::img_key($u[0]); }
                $alt = null; foreach ($keys as $k) if ($k !== '' && isset($want[$k])) { $alt = $want[$k]; break; }
                if ($alt === null) return $tag;
                $base = preg_replace('/\s+alt\s*=\s*(?:"[^"]*"|\'[^\']*\')/i', '', $tag, 1);
                $base = preg_replace('/\s+alt(?=[\s\/>])(?!\s*=)/i', '', $base, 1);
                $self = (bool)preg_match('#/>$#', rtrim($base)); $base = rtrim(preg_replace('#/?>$#', '', rtrim($base)));
                return $base . ' alt="' . esc_attr($alt) . '" data-sab="' . (int)$mid . '"' . ($self ? ' />' : '>');
            }, $part);
        }
        return implode('', $parts);
    }

    /** Link the first plain-text occurrence of $anchor (never inside links, headings, buttons or scripts). Pure function. */
    static function link_first($html, $anchor, $url, $mid) {
        $skiptags = ['a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'button', 'script', 'style', 'textarea', 'select', 'option', 'svg', 'noscript'];
        $parts = preg_split('/(<[^>]*>)/', $html, -1, PREG_SPLIT_DELIM_CAPTURE); $skip = 0; $done = false;
        $re = '/(?<![\w-])(' . preg_quote($anchor, '/') . ')(?![\w-])/iu';
        foreach ($parts as $i => $tok) {
            if ($tok === '') continue;
            if ($tok[0] === '<') {
                if (preg_match('#^<\s*(/?)\s*([a-z0-9]+)#i', $tok, $m) && in_array(strtolower($m[2]), $skiptags, true)) {
                    if ($m[1] === '/') $skip = max(0, $skip - 1); elseif (substr($tok, -2) !== '/>') $skip++;
                }
                continue;
            }
            if ($done || $skip > 0 || !preg_match($re, $tok)) continue;
            $parts[$i] = preg_replace_callback($re, function ($m) use ($url, $mid) { return '<a href="' . esc_url($url) . '" data-sab="' . (int)$mid . '">' . $m[1] . '</a>'; }, $tok, 1);
            $done = true;
        }
        return $done ? implode('', $parts) : $html;
    }

    static function missing_meta(WP_REST_Request $r) {
        $q = new WP_Query(['post_type' => ['page', 'post'], 'post_status' => 'publish', 'posts_per_page' => 500, 'fields' => 'ids', 'no_found_rows' => true]);
        $out = [];
        foreach ($q->posts as $id) { $s = self::get_seo($id);
            if ($s['title'] === '' || $s['description'] === '') $out[] = ['post_id' => $id, 'url' => get_permalink($id), 'has_title' => $s['title'] !== '', 'has_description' => $s['description'] !== '']; }
        return ['count' => count($out), 'items' => $out];
    }

    static function walk($els, &$acc) {
        foreach ((array)$els as $e) {
            $wt = $e['widgetType'] ?? ''; $s = $e['settings'] ?? [];
            if ($wt === 'heading') { $acc['headings'][] = ['level' => strtolower($s['header_size'] ?? 'h2'), 'text' => trim(wp_strip_all_tags($s['title'] ?? ''))]; }
            if ($wt === 'image') { $acc['images']++; if (empty($s['image']['alt']) && empty(get_post_meta((int)($s['image']['id'] ?? 0), '_wp_attachment_image_alt', true))) $acc['images_no_alt']++; }
            foreach (['editor', 'html'] as $k) if (!empty($s[$k]) && is_string($s[$k])) {
                $acc['text'] .= ' ' . $s[$k];
                if (preg_match_all('#<script[^>]+application/ld\+json[^>]*>(.*?)</script>#is', $s[$k], $m)) foreach ($m[1] as $j) {
                    $d = json_decode($j, true); $acc['schema'][] = ['valid' => $d !== null, 'has_aggregate_rating' => stripos($j, 'AggregateRating') !== false];
                }
            }
            if (!empty($e['elements'])) self::walk($e['elements'], $acc);
        }
    }

    static function audit(WP_REST_Request $r) {
        $id = (int)$r['post_id']; $p = get_post($id); if (!$p) return new WP_Error('sab_nf', 'not found', ['status' => 404]);
        $acc = ['headings' => [], 'images' => 0, 'images_no_alt' => 0, 'text' => '', 'schema' => []];
        $raw = get_post_meta($id, '_elementor_data', true);
        $data = is_string($raw) ? json_decode($raw, true) : $raw;
        if (is_array($data)) self::walk($data, $acc); else $acc['text'] = $p->post_content;
        $txt = wp_strip_all_tags($acc['text']); $h = $acc['headings'];
        $count = function ($lvl) use ($h) { return count(array_filter($h, function ($x) use ($lvl) { return $x['level'] === $lvl; })); };
        preg_match_all('/(100%|guarantee|painless|scarless|USFDA|FDA[- ]approved|NABH)/i', $txt, $cl);
        $seo = self::get_seo($id);
        return ['post_id' => $id, 'url' => get_permalink($id), 'title' => $seo['title'], 'description' => $seo['description'],
            'builder' => is_array($data) ? 'elementor' : 'classic', 'words' => str_word_count($txt),
            'h1' => $count('h1'), 'h2' => $count('h2'), 'h3' => $count('h3'), 'headings' => array_slice($h, 0, 60),
            'images' => $acc['images'], 'images_no_alt' => $acc['images_no_alt'], 'schema' => $acc['schema'],
            'risky_claims' => array_values(array_unique(array_map('strtolower', $cl[0]))), 'modified' => $p->post_modified_gmt];
    }
}
SAB::init();
