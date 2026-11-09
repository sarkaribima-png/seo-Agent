"""Import the real wp/static/onpage/briefs modules once, before tests/test_flow.py swaps seo_agent.wp, .gsc and .propose for fakes in sys.modules.
The package attribute 'seo_agent.wp' is removed afterwards so that test_flow's fake is still the one `from . import wp` finds inside run.daily()."""
import os, sys, tempfile
os.environ.setdefault("SEO_DB", tempfile.mktemp())   # seo_agent.db reads this when first imported; never touch data/state.db from tests
import seo_agent
import seo_agent.wp, seo_agent.static, seo_agent.onpage, seo_agent.briefs, seo_agent.placement   # noqa: E401
for _n in ("wp",):
    if hasattr(seo_agent, _n): delattr(seo_agent, _n)
