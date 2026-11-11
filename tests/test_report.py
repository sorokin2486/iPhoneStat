import json
from datetime import datetime

from iphonestat.__main__ import main
from iphonestat.demo import build_demo_scan
from iphonestat.device import DeviceInfo, ScanResult
from iphonestat.report import DATA_PLACEHOLDER, render_report, render_waiting
from iphonestat.tree import KIND_DIR, KIND_FREE, KIND_HIDDEN, KIND_OTHER, Node, group, leaf

SCANNED_AT = datetime(2026, 11, 10, 12, 30)


def _extract_payload(html: str) -> dict:
    start = html.index("const REPORT = ") + len("const REPORT = ")
    end = html.index(";\n", start)
    return json.loads(html[start:end])


def test_render_report_embeds_device_and_tree_as_json():
    scan = ScanResult(
        device=DeviceInfo(name="Мой iPhone", model="iPhone15,2", ios_version="18.1"),
        root=group("Мой iPhone", KIND_DIR, [leaf("a.bin", 42, KIND_OTHER)]),
    )

    html = render_report(scan, SCANNED_AT)

    payload = _extract_payload(html)
    assert DATA_PLACEHOLDER not in html
    assert payload["device"] == {"name": "Мой iPhone", "model": "iPhone15,2", "ios": "18.1"}
    assert payload["scannedAt"] == "10.11.2026 12:30"
    assert payload["tree"]["c"][0] == {"n": "a.bin", "s": 42, "k": KIND_OTHER}


def test_render_report_neutralises_markup_in_file_names():
    hostile_name = "</script><script>alert(1)</script>"
    scan = ScanResult(
        device=DeviceInfo(name="iPhone", model="m", ios_version="1"),
        root=group("iPhone", KIND_DIR, [leaf(hostile_name, 1, KIND_OTHER)]),
    )

    html = render_report(scan, SCANNED_AT)

    assert hostile_name not in html
    assert _extract_payload(html)["tree"]["c"][0]["n"] == hostile_name


def test_demo_scan_accounts_for_every_byte_of_the_disk():
    scan = build_demo_scan()

    kinds = {child.kind: child.size for child in scan.root.children}
    assert scan.root.size == 256 * 1000**3
    assert kinds[KIND_FREE] == 38 * 1000**3
    assert kinds[KIND_HIDDEN] > 0


def test_waiting_page_carries_the_token_for_status_requests():
    page = render_waiting("secret-token")

    assert 'const TOKEN = "secret-token";' in page
    assert "__APP_TOKEN__" not in page


def test_main_in_demo_mode_writes_report_file(tmp_path):
    output = tmp_path / "out" / "report.html"

    exit_code = main(["--demo", "--report-only", "--output", str(output)])

    assert exit_code == 0
    assert "iPhone (демо)" in _extract_payload(output.read_text(encoding="utf-8"))["device"]["name"]


def test_render_report_enables_live_actions_only_when_token_is_given():
    scan = build_demo_scan()

    assert _extract_payload(render_report(scan, SCANNED_AT))["live"] is None
    assert _extract_payload(render_report(scan, SCANNED_AT, live_token="secret"))["live"] == {"token": "secret"}


def test_report_carries_device_address_only_at_walk_roots():
    scan = ScanResult(
        device=DeviceInfo(name="iPhone", model="m", ios_version="1"),
        root=group("iPhone", KIND_DIR, [Node("Медиа", 5, KIND_DIR, (leaf("a.bin", 5, KIND_OTHER),), source="afc:/")]),
    )

    media = _extract_payload(render_report(scan, SCANNED_AT))["tree"]["c"][0]

    assert media["p"] == "afc:/"
    assert "p" not in media["c"][0]
