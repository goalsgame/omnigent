"""Published HTML isolation and sharing in a real browser with a sealed backend."""

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.conftest import BrowserContract

_ARTIFACT = "a" * 32
_HTML = """<!doctype html><html><body>
<button onclick="this.textContent='Interaction works'">Try interaction</button>
<div id="isolation"></div>
<script>
let parentBlocked=false,storageBlocked=false;
try{parent.document.body.dataset.compromised='yes'}catch{parentBlocked=true}
try{localStorage.getItem('test-secret')}catch{storageBlocked=true}
fetch('/v1/me').then(()=>document.querySelector('#isolation').textContent='fetch leaked')
.catch(()=>document.querySelector('#isolation').textContent=
JSON.stringify({parentBlocked,storageBlocked,fetchBlocked:true}));
</script></body></html>"""


@pytest.fixture
def artifact_url(browser_contract: BrowserContract):
    empty = {"object": "list", "data": [], "first_id": None, "last_id": None, "has_more": False}
    browser_contract.json(
        "/v1/info",
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "features": {},
            "ui": {
                "branding": {"name": None, "url": None, "logo": None, "favicon": None},
                "powered_by": True,
            },
        },
    )
    browser_contract.json("/v1/me", {"user_id": "local", "is_admin": True})
    for path in ("agents", "hosts", "projects", "extensions", "sessions"):
        browser_contract.json(f"/v1/{path}", empty)
    browser_contract.json(
        "/v1/projects/order", {"ordered_project_ids": None, "sort_mode": "alphabetical"}
    )
    browser_contract.json("/v1/harnesses", {"data": [], "setup_steps": {}})
    browser_contract.json("/v1/sessions/projects", [])
    browser_contract.websocket("**/v1/sessions/updates*", lambda ws: ws.on_message(lambda _: None))
    publication = {
        "id": _ARTIFACT,
        "title": "Isolated report",
        "filename": "report.html",
        "kind": "html",
        "byte_size": len(_HTML),
        "expires_at": 2000000000,
        "can_manage": True,
        "grants": [],
    }
    browser_contract.json(f"/v1/artifacts/{_ARTIFACT}", publication)
    browser_contract.response(f"/v1/artifacts/{_ARTIFACT}/preview", status=200, body=_HTML)
    browser_contract.json("/v1/artifacts", {"data": [publication]})
    return browser_contract.base_url + f"/artifacts/{_ARTIFACT}"


def test_html_scripts_cannot_read_parent_storage_or_call_api(page: Page, artifact_url: str):
    page.goto(artifact_url)
    expect(page.get_by_role("heading", name="Isolated report")).to_be_visible()
    frame = page.frame_locator('iframe[title="Published HTML artifact"]')
    expect(frame.locator("#isolation")).to_have_text(
        '{"parentBlocked":true,"storageBlocked":true,"fetchBlocked":true}'
    )
    assert frame.locator("html").evaluate("() => document.compatMode") == "CSS1Compat"
    frame.get_by_role("button", name="Try interaction").click()
    expect(frame.get_by_role("button", name="Interaction works")).to_be_visible()
    assert page.evaluate("document.body.dataset.compromised") is None
    expect(page.locator('iframe[title="Published HTML artifact"]')).to_have_attribute(
        "sandbox", "allow-scripts"
    )


def test_catalog_navigation_and_group_sharing(
    page: Page, artifact_url: str, browser_contract: BrowserContract
):
    received = []

    def saved(request):
        received.append(request.post_data_json)
        return {
            "id": _ARTIFACT,
            "title": "Isolated report",
            "filename": "report.html",
            "kind": "html",
            "byte_size": 10,
            "expires_at": 2000000000,
            "can_manage": True,
            "grants": request.post_data_json["grants"],
        }

    browser_contract.json(f"/v1/artifacts/{_ARTIFACT}/permissions", saved, method="PUT")
    page.goto(artifact_url)
    page.get_by_text("Sharing", exact=True).click()
    page.get_by_label("Principal type").select_option("group")
    page.get_by_label("User identity or group name").fill("engineering")
    page.get_by_role("button", name="Grant read access").click()
    expect(page.get_by_text("group: engineering", exact=False)).to_be_visible()
    assert received == [{"grants": [{"principal_type": "group", "principal": "engineering"}]}]
    page.get_by_test_id("artifacts-nav").click()
    expect(page.get_by_role("heading", name="Published artifacts")).to_be_visible()
    page.get_by_role("link", name="Isolated report").click()
    expect(page.get_by_role("heading", name="Isolated report")).to_be_visible()


def test_long_catalog_scrolls_inside_shell(
    page: Page, artifact_url: str, browser_contract: BrowserContract
):
    rows = [
        {
            "id": f"{i:032x}",
            "title": f"Report {i}",
            "filename": "x",
            "kind": "file",
            "byte_size": 1,
            "expires_at": 2000000000,
            "can_manage": True,
        }
        for i in range(100)
    ]
    browser_contract.json("/v1/artifacts", {"data": rows})
    page.goto(browser_contract.base_url + "/artifacts")
    last = page.get_by_role("link", name="Report 99", exact=True)
    last.scroll_into_view_if_needed()
    expect(last).to_be_visible()
    scroller = page.get_by_test_id("artifacts-scroll")
    assert scroller.evaluate("el => el.scrollHeight > el.clientHeight")
    assert scroller.evaluate("el => el.scrollTop > 0")
