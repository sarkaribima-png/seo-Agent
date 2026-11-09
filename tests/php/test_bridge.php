<?php
// Run: php tests_php/test_bridge.php   (stubs the few WordPress functions the pure functions use)
define('ABSPATH', __DIR__);
function add_action(...$a) {} function add_filter(...$a) {}
function wp_strip_all_tags($s) { return strip_tags((string)$s); }
function esc_attr($s) { return htmlspecialchars((string)$s, ENT_QUOTES, 'UTF-8'); }
function esc_url($s) { return $s; } function esc_url_raw($s) { return (string)$s; }
function sanitize_text_field($s) { return trim(strip_tags((string)$s)); }
function wp_kses_post($s) { return $s; } function wp_json_encode($x, $f = 0) { return json_encode($x, $f); }
function is_wp_error($x) { return $x instanceof WP_Error; }
class WP_Error { public $c, $m; function __construct($c = '', $m = '') { $this->c = $c; $this->m = $m; } }
class WP_REST_Request {} class WP_Query {}
require __DIR__ . '/../../wp-plugin/seo-agent-bridge/seo-agent-bridge.php';
$fail = 0; function ok($c, $m) { global $fail; if (!$c) { $fail++; echo "FAIL: $m\n"; } else echo "ok:   $m\n"; }

$page = '<div class="e"><div class="w"><h2 class="t">Overview</h2></div><p>intro</p>'
      . '<div class="w"><div class="x"><h2 class="t">Recovery &amp; care</h2></div></div><p>rec</p><h3>Day one</h3><p>d1</p>'
      . '<div class="w"><h2>Frequently asked questions</h2></div><h3>Q1</h3><p>a</p>'
      . '<div class="w"><div class="x"><h2>Contact</h2></div></div></div>';
$B = '<section data-sab="9">NEW</section>';

$r = SAB::inject_section($page, 'NEW', 9, ['after_heading' => 'Overview']);
ok(strpos($r, 'intro</p><div class="w"><div class="x"><h2 class="t">Recovery') === false && strpos($r, 'intro</p><section class="sab-section" data-sab="9">NEW</section><div class="w"><div class="x"><h2 class="t">Recovery') !== false, 'after_heading places block before the next heading wrapper (steps out of 2 wrappers)');

$r = SAB::inject_section($page, 'NEW', 9, ['after_heading' => 'recovery & CARE']);
ok(strpos($r, '<p>d1</p><section class="sab-section" data-sab="9">NEW</section><div class="w"><h2>Frequently') !== false, 'case and entity insensitive match; h3 subsection stays inside the h2 block');

$r = SAB::inject_section($page, 'NEW', 9, ['after_heading' => 'Day one']);
ok(strpos($r, '<p>d1</p><section class="sab-section" data-sab="9">NEW</section><div class="w"><h2>Frequently') !== false, 'h3 target ends at next heading of same or higher level');

$r = SAB::inject_section($page, 'NEW', 9, ['after_heading' => 'Contact']);
ok(substr($r, -strlen('NEW</section>')) === 'NEW</section>', 'last heading: block goes at the end');

$r = SAB::inject_section($page, 'NEW', 9, ['after_heading' => 'Recovery']);
ok(strpos($r, 'data-sab="9"') !== false, 'unique partial match is accepted');

$two = '<h2>Tips</h2><p>a</p><h2>Tips</h2><p>b</p><h2>End</h2>';
$r = SAB::inject_section($two, 'NEW', 9, ['after_heading' => 'Tips', 'occurrence' => 2]);
ok(strpos($r, '<p>b</p><section class="sab-section" data-sab="9">NEW</section><h2>End') !== false, 'occurrence 2');
$r = SAB::inject_section($two, 'NEW', 9, ['after_heading' => 'Missing heading']);
ok($r === $two, 'heading not found: content unchanged (agent live check will roll back)');
$r = SAB::inject_section('<h2>Tips a</h2><h2>Tips b</h2>', 'NEW', 9, ['after_heading' => 'Tips']);
ok(strpos($r, 'data-sab') === false, 'ambiguous partial match: unchanged');

// old positions still work
$r = SAB::inject_section($page, 'NEW', 9, 'before_faq'); ok(strpos($r, 'NEW</section><div class="w"><h2>Frequently') === false && strpos($r, 'data-sab="9">NEW</section><h2>Frequently') !== false || strpos($r, 'NEW</section><h2>Frequently') !== false, 'before_faq unchanged behaviour');
$r = SAB::inject_section($page, 'NEW', 9, 'end'); ok(substr($r, -strlen('NEW</section>')) === 'NEW</section>', 'end unchanged behaviour');

// clean_position
ok(SAB::clean_position(null) === 'before_faq' && SAB::clean_position('bogus') === 'before_faq' && SAB::clean_position('end') === 'end', 'clean_position strings');
$c = SAB::clean_position(['after_heading' => '  Recovery   ', 'occurrence' => 3]); ok($c === ['after_heading' => 'Recovery', 'occurrence' => 3], 'clean_position object');
ok(is_wp_error(SAB::clean_position(['after_heading' => ' '])), 'clean_position rejects empty heading');
ok(!is_wp_error(SAB::clean_mod('section', ['html' => '<h2>x</h2><p>y</p>', 'position' => ['after_heading' => 'Overview']])), 'clean_mod section accepts after_heading');

// alt text
$h = '<p><img src="https://x.in/wp-content/uploads/2025/01/gyno-grade-3-300x200.jpg" alt="" width="300" height="200"></p>'
   . '<img data-src="/wp-content/uploads/clinic-room.webp" src="data:image/gif;base64,AAA" alt>'
   . '<img src="/a/kept.jpg" alt="Already written">'
   . '<img src="/a/nomatch.jpg" alt="">'
   . '<script>var s = \'<img src="/a/gyno-grade-3.jpg" alt="">\';</script><!-- <img src="/a/gyno-grade-3.jpg" alt=""> -->';
$items = [['src' => '/wp-content/uploads/2025/01/gyno-grade-3.jpg', 'alt' => 'Chest diagram showing gynecomastia grade 3'], ['src' => 'https://x.in/wp-content/uploads/clinic-room.webp', 'alt' => 'Consultation room with a desk and two chairs'], ['src' => '/a/kept.jpg', 'alt' => 'Should not overwrite']];
$o = SAB::alt_apply($h, $items, 7);
ok(strpos($o, 'alt="Chest diagram showing gynecomastia grade 3" data-sab="7"') !== false, 'alt filled when size suffix differs');
ok(strpos($o, 'data-src="/wp-content/uploads/clinic-room.webp" src="data:image/gif;base64,AAA" alt="Consultation room with a desk and two chairs" data-sab="7">') !== false, 'lazy-load data-src matched, bare alt attribute replaced');
ok(strpos($o, 'alt="Already written">') !== false && strpos($o, 'Should not overwrite') === false, 'existing alt never overwritten');
ok(strpos($o, '<img src="/a/nomatch.jpg" alt="">') !== false, 'non matching image untouched');
ok(substr_count($o, 'data-sab="7"') === 2, 'script and comment copies untouched');
$o2 = SAB::alt_apply($o, $items, 7); ok($o2 === $o, 'idempotent');
ok(is_wp_error(SAB::clean_mod('alt', ['items' => [['src' => '/a/b.jpg', 'alt' => 'Before and after of a patient']]])), 'alt with claim rejected');
ok(is_wp_error(SAB::clean_mod('alt', ['items' => [['src' => '/a/b.jpg', 'alt' => 'x']]])), 'alt too short rejected');
$g = SAB::clean_mod('alt', ['items' => [['src' => '/a/b.jpg', 'alt' => 'Clinic reception desk']]]); ok(!is_wp_error($g) && $g['items'][0]['alt'] === 'Clinic reception desk', 'valid alt accepted');
echo $fail ? "\n$fail FAILED\n" : "\nALL PHP TESTS PASSED\n"; exit($fail ? 1 : 0);
