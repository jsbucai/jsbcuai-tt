"""Collect Beefun's unfinished purchase report through its ordinary HTTPS UI.

This is an isolated read/download tool. It has no ERP write actions, backend
imports, Android hooks, or scheduler. Browser selectors require a supervised
Linux trial before operational use.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .validator import ExportValidationError, validate_export


class CollectorError(RuntimeError):
    """A safe, non-secret failure code for a halted browser collection."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _allowed_https(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        suffixes = ("wsgjp.com", "wsgjp.com.cn", "mygjp.com.cn")
        return (
            parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.port in (None, 443)
            and any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)
        )
    except ValueError:
        return False


def _official_login_entry(url: str) -> bool:
    """The password page advertised by the public WSGJP home page."""
    if not _allowed_https(url):
        return False
    parsed = urlsplit(url)
    return parsed.hostname == "www.mygjp.com.cn" and parsed.path == "/ngpLogin.aspx"


def _official_password_frame(url: str) -> bool:
    if not _allowed_https(url):
        return False
    parsed = urlsplit(url)
    return parsed.hostname == "passport.mygjp.com.cn" and parsed.path == "/erp/ngploginNew"


def _erp_main(url: str) -> bool:
    return _allowed_https(url) and urlsplit(url).path.lower().endswith("/main.gspx")


def _account_scope(company: str, erp_main_url: str) -> str:
    """Stable local scope for one verified login tenant, without storing its name."""
    if not isinstance(company, str) or not company.strip() or not _erp_main(erp_main_url):
        raise CollectorError("account_scope_unverified")
    identity = ["beefun_login_tenant_v1", urlsplit(erp_main_url).hostname.lower(),
                company.strip().casefold()]
    return sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()


def _read_credentials(path: Path) -> dict[str, str]:
    # Never accept credentials on the command line or echo them in errors.
    if path.is_symlink() or not path.is_file():
        raise CollectorError("credentials_file_missing_or_symlink")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise CollectorError("credentials_file_permissions_not_private")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise CollectorError("credentials_file_invalid_json") from None
    if not isinstance(raw, dict) or any(not isinstance(raw.get(k), str) or not raw[k].strip()
                                        for k in ("login_url", "company", "username", "password")):
        raise CollectorError("credentials_file_missing_fields")
    if not _allowed_https(raw["login_url"]):
        raise CollectorError("login_url_not_official_https")
    return {key: raw[key] for key in ("login_url", "company", "username", "password")}


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise CollectorError("output_directory_symlink")
    path.chmod(0o700)


def _write_status(paths: list[Path], status: str, started_at: str,
                  reason: str = "") -> None:
    if status not in {"running", "needs_human", "failed", "succeeded"}:
        raise CollectorError("invalid_collector_status")
    if reason and not re.fullmatch(r"[a-z0-9_]{1,80}", reason):
        reason = "unexpected_automation_error"
    payload = {"status": status, "started_at_utc": started_at,
               "updated_at_utc": _now(), "reason": reason}
    for path in dict.fromkeys(paths):
        _private_dir(path.parent)
        if path.is_symlink():
            raise CollectorError("status_file_symlink")
        staged = path.with_name("." + path.name + "." + uuid4().hex)
        staged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        staged.chmod(0o600)
        os.replace(staged, path)


def _visible_dialog(page, title: str):
    dialogs = page.locator(".FormBorder:visible").filter(has_text=title).all()
    onscreen = []
    for dialog in dialogs:
        box = dialog.bounding_box()
        if box and box["x"] + box["width"] > 0 and box["y"] + box["height"] > 0 \
                and box["x"] < page.viewport_size["width"] \
                and box["y"] < page.viewport_size["height"]:
            onscreen.append(dialog)
    if len(onscreen) != 1:
        raise CollectorError("expected_dialog_missing")
    return onscreen[0]


def _click_exact(page, label: str, *, dialog: str | None = None) -> None:
    """Click one visible, exact-label control; fail on missing UI structure."""
    try:
        point = page.wait_for_function(
        """({label, dialog}) => {
          const visible = el => {const r=el.getBoundingClientRect(),s=getComputedStyle(el);
            if(!(r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'))return false;
            const x=r.left+r.width/2,y=r.top+r.height/2;
            if(x<0||y<0||x>=innerWidth||y>=innerHeight)return false;
            const top=document.elementFromPoint(x,y);
            return !!top&&(top===el||el.contains(top));};
          let root=document;
          if (dialog) {
            const matches=[...document.querySelectorAll('.FormBorder')]
              .filter(el=>visible(el) && (el.innerText||'').includes(dialog));
            root=matches[matches.length-1];
            if (!root) return null;
          }
          const candidates=[...root.querySelectorAll('button,input[type=button],a,label,span,div')]
            .filter(visible).filter(el=>String(el.tagName==='INPUT'?el.value:el.innerText||'')
              .replace(/\\s+/g,' ').trim()===label)
            .sort((a,b)=>score(b)-score(a));
          function score(el) {
            const r=el.getBoundingClientRect(),id=String(el.id||'');
            let value=0;
            if(el.matches('button,input[type=button]'))value+=1000;
            if(el.matches('a,label,span'))value+=300;
            if(dialog && /(^|\\$)(btnOk|btnOK|btnSure|btnConfirm|btnSearch|btnQuery|btnFilter)$/i.test(id))value+=3000;
            if(dialog && label==='筛选' && /(^|\\$)btnMoveUp$/i.test(id))value+=6000;
            if(dialog && /(^|\\$)(btnMoveDown|btnMoveLeft|btnMoveRight|btnAdd|btnRemove)$/i.test(id))value-=5000;
            if(dialog)value+=Math.max(0,r.top-root.getBoundingClientRect().top)/2;
            value-=Math.min(r.width*r.height/1000,300);
            return value;
          }
          if (!candidates.length) return null;
          const r=candidates[0].getBoundingClientRect();
          return {x:r.left+r.width/2,y:r.top+r.height/2};
        }""",
            arg={"label": label, "dialog": dialog}, timeout=15_000,
        ).json_value()
    except Exception:
        raise CollectorError("ui_control_missing")
    page.mouse.click(point["x"], point["y"])


def _wait_dialog(page, title: str, timeout_ms: int = 15_000) -> None:
    try:
        page.wait_for_function(
            """title => [...document.querySelectorAll('.FormBorder')].some(el => {
              const r=el.getBoundingClientRect(),s=getComputedStyle(el);
              return r.width>0&&r.height>0&&r.left<innerWidth&&r.top<innerHeight&&
                r.right>0&&r.bottom>0&&s.display!=='none'&&s.visibility!=='hidden'&&
                (el.innerText||'').includes(title);
            })""", arg=title, timeout=timeout_ms)
    except Exception:
        raise CollectorError("expected_dialog_missing") from None


def _wait_onscreen_text(page, label: str, timeout_ms: int = 60_000) -> None:
    """A previous report remains in the DOM after Beefun moves it off-screen."""
    try:
        page.wait_for_function(
            """label => [...document.querySelectorAll('button,div,span,td,h1,h2')].some(el => {
              if((el.innerText||'').trim()!==label)return false;
              const r=el.getBoundingClientRect(),s=getComputedStyle(el);
              return r.width>0&&r.height>0&&r.left<innerWidth&&r.top<innerHeight&&
                r.right>0&&r.bottom>0&&s.display!=='none'&&s.visibility!=='hidden';
            })""", arg=label, timeout=timeout_ms)
    except Exception:
        raise CollectorError("report_list_not_open") from None


def _web_row_count(page) -> int:
    _wait_onscreen_text(page, "进货订单统计列表", timeout_ms=10_000)
    try:
        counts = page.wait_for_function(
        """() => {const counts=[...document.querySelectorAll('.PagerSummaryText')]
          .filter(el => {const r=el.getBoundingClientRect(),s=getComputedStyle(el);
            return r.width>0&&r.height>0&&r.left<innerWidth&&r.top<innerHeight&&
              r.right>0&&r.bottom>0&&s.display!=='none'&&s.visibility!=='hidden';})
          .map(el => /^\\(\\s*([\\d,]+)\\s*条记录\\s*\\)$/.exec((el.innerText||'').trim()))
          .filter(Boolean).map(match => Number(match[1].replace(/,/g,'')));
          return counts.length===1?counts:null;}""", timeout=30_000,
        ).json_value()
    except Exception:
        raise CollectorError("web_row_count_missing_or_ambiguous") from None
    if len(counts) != 1 or not isinstance(counts[0], int):
        raise CollectorError("web_row_count_missing_or_ambiguous")
    return counts[0]


def _stable_web_row_count(page) -> int:
    """Read a settled pager after its matching summary response completes."""
    previous = None
    equal_reads = 0
    for _ in range(30):
        count = _web_row_count(page)
        equal_reads = equal_reads + 1 if count == previous else 1
        if equal_reads >= 3:
            return count
        previous = count
        page.wait_for_timeout(300)
    raise CollectorError("web_row_count_unstable")


def _open_password_frame(page):
    # Main.gspx redirects an unauthenticated session to an HTTP marketing
    # page. Resolve the current HTTPS password entry from the official home
    # page instead of ever following that redirect with credentials in hand.
    page.goto("https://www.wsgjp.com.cn/", wait_until="domcontentloaded", timeout=45_000)
    if not _allowed_https(page.url) or urlsplit(page.url).hostname != "www.wsgjp.com.cn":
        raise CollectorError("homepage_redirect_not_official_https")
    login = page.locator("#md_login")
    try:
        login.wait_for(state="visible", timeout=20_000)
        entry = login.get_attribute("data-url") or ""
    except Exception:
        raise CollectorError("official_login_link_missing") from None
    if not _official_login_entry(entry):
        raise CollectorError("official_login_link_unexpected")
    # Navigating to the URL published by the header uses the same password
    # page without creating a second tab, so report work continues in `page`.
    page.goto(entry, wait_until="domcontentloaded", timeout=45_000)
    if not _official_login_entry(page.url):
        raise CollectorError("login_redirect_not_official_https")
    for _ in range(40):
        frame = next((item for item in page.frames if _official_password_frame(item.url)), None)
        if frame is not None:
            password_tab = frame.locator("#tb1")
            if password_tab.count() == 1 and password_tab.is_visible():
                password_tab.click()
                break
        page.wait_for_timeout(500)
    else:
        raise CollectorError("password_login_tab_missing")
    for _ in range(40):
        controls = [frame.locator("#company"), frame.locator("#username"),
                    frame.locator("#password"), frame.locator("#btnlogin")]
        if all(item.count() == 1 and item.is_visible() for item in controls):
            if not _official_login_entry(page.url) or not _official_password_frame(frame.url):
                raise CollectorError("login_redirect_not_official_https")
            return frame, controls
        page.wait_for_timeout(500)
    raise CollectorError("login_form_missing_or_manual_verification")


def _ready_erp_page(page):
    for candidate in page.context.pages:
        if _erp_main(candidate.url):
            try:
                candidate.locator('.MenuRootCaption', has_text='进货统计').first.wait_for(
                    state='visible', timeout=30_000)
            except Exception:
                raise CollectorError('erp_main_not_ready') from None
            return candidate
    return None


def _login_failure(page):
    for candidate in page.context.pages:
        for frame in candidate.frames:
            if not _allowed_https(frame.url): continue
            try:
                body = frame.locator('body').inner_text(timeout=2000)[:10000]
            except Exception:
                continue
            if 'HTTP Error 503' in body or 'Service Unavailable' in body:
                raise CollectorError('erp_service_unavailable')
            if any(word in body for word in ('密码错误', '密码不正确', '用户名或密码错误')):
                raise CollectorError('login_password_rejected')


def _wait_manual_login(page, status_paths: list[Path], started_at: str, human_handler=None):
    # Do not submit again, solve a challenge, close the browser, or turn a
    # CAPTCHA into success. The headed isolated session stays available to
    # the operator until login completes or they explicitly stop the service.
    _login_failure(page)
    _write_status(status_paths, "needs_human", started_at,
                  "manual_login_verification_required")
    if human_handler is not None:
        return human_handler(page)
    heartbeat = time.monotonic()
    while True:
        _login_failure(page)
        ready = _ready_erp_page(page)
        if ready is not None:
            _write_status(status_paths, "running", started_at)
            return ready
        pages = [candidate for candidate in page.context.pages if not candidate.is_closed()]
        if not pages:
            raise CollectorError("manual_login_session_closed")
        if any(candidate.url.startswith("http://") for candidate in pages):
            raise CollectorError("login_redirect_not_official_https")
        pages[0].wait_for_timeout(1000)
        if time.monotonic() - heartbeat >= 30:
            _write_status(status_paths, "needs_human", started_at,
                          "manual_login_verification_required")
            heartbeat = time.monotonic()


def _login(page, credentials: dict[str, str], *, wait_for_human: bool = False,
           status_paths: list[Path] | None = None, started_at: str = "", human_handler=None):
    try:
        frame, controls = _open_password_frame(page)
    except CollectorError as error:
        if wait_for_human and str(error) in {"login_form_missing_or_manual_verification",
                                             "password_login_tab_missing"}:
            return _wait_manual_login(page, status_paths or [], started_at or _now(), human_handler)
        raise
    controls[0].fill(credentials["company"])
    controls[1].fill(credentials["username"])
    controls[2].fill(credentials["password"])
    controls[3].click()
    for _ in range(45):
        _login_failure(page)
        ready = _ready_erp_page(page)
        if ready is not None:
            return ready
        for candidate in page.context.pages:
            if candidate.url.startswith("http://"):
                raise CollectorError("login_redirect_not_official_https")
        try:
            body = frame.locator("body").inner_text(timeout=2_000)
        except Exception:
            body = ""
        if any(word in body for word in ("滑块", "短信验证", "人机验证", "安全验证", "输入验证码")):
            if wait_for_human:
                return _wait_manual_login(page, status_paths or [], started_at or _now(), human_handler)
            raise CollectorError("manual_login_verification_required")
        page.wait_for_timeout(1_000)
    if wait_for_human:
        return _wait_manual_login(page, status_paths or [], started_at or _now(), human_handler)
    raise CollectorError("login_not_completed")


def _select_unfinished_column(page) -> None:
    dialog = _visible_dialog(page, "筛选")
    column = dialog.locator('input[id$="$edColumns"]:visible')
    if column.count() != 1:
        raise CollectorError("filter_column_control_missing")
    # The old Beefun control is a custom drop-down. Click its adjacent arrow,
    # then select the named option. Never assume a fixed keyboard offset.
    arrow = column.evaluate(
        """el=>{const parent=el.closest('.EditBlock'),arrow=parent&&
          [...parent.querySelectorAll('.SkinButton')].find(x=>x!==el);
          if(!arrow)return null;const r=arrow.getBoundingClientRect();
          return {x:r.left+r.width/2,y:r.top+r.height/2};}"""
    )
    if not arrow:
        raise CollectorError("filter_dropdown_arrow_missing")
    page.mouse.click(arrow["x"], arrow["y"])
    option = page.locator(".DropPopup:visible").get_by_text("未完成数量", exact=True)
    if option.count() != 1:
        raise CollectorError("filter_option_missing_or_ambiguous")
    option.click()
    if column.input_value().strip() != "未完成数量":
        raise CollectorError("filter_column_not_selected")


def _open_filtered_report(page, *, start: date, end: date, threshold: Decimal) -> int:
    for attempt in range(2):
        _click_exact(page, "进货统计")
        try:
            _wait_dialog(page, "进货订单统计条件", timeout_ms=20_000)
            break
        except CollectorError:
            if attempt:
                raise
    # The dialog shell appears before its radio options are populated.
    unit = _visible_dialog(page, "进货订单统计条件").locator(
        '.RadioButtonList[id$="$listType"] label:visible', has_text="单位方式")
    try:
        unit.wait_for(state="visible", timeout=20_000)
    except Exception:
        raise CollectorError("unit_way_control_missing") from None
    if unit.count() != 1 or unit.inner_text().strip() != "单位方式":
        raise CollectorError("unit_way_control_ambiguous")
    unit.click()
    selected = _visible_dialog(page, "进货订单统计条件").locator('input[id$="$listType_radio1"]')
    if selected.count() != 1 or not selected.is_checked():
        raise CollectorError("unit_way_not_selected")
    dialog = _visible_dialog(page, "进货订单统计条件")
    begin = dialog.locator('input[id$="$beginDate"]:visible')
    finish = dialog.locator('input[id$="$endDate"]:visible')
    if begin.count() != 1 or finish.count() != 1:
        raise CollectorError("date_controls_missing_or_ambiguous")
    begin.fill(start.isoformat())
    finish.fill(end.isoformat())
    _click_exact(page, "确定", dialog="进货订单统计条件")
    _wait_onscreen_text(page, "详情")
    for attempt in range(2):
        _click_exact(page, "详情")
        try:
            _wait_dialog(page, "全部列表", timeout_ms=20_000)
            break
        except CollectorError:
            if attempt:
                raise
    _click_exact(page, "全部列表", dialog="全部列表")
    selected_all = _visible_dialog(page, "全部列表").locator('input[id$="$listAll"]')
    if selected_all.count() != 1 or not selected_all.is_checked():
        raise CollectorError("all_rows_not_selected")
    _click_exact(page, "确定", dialog="全部列表")
    _wait_onscreen_text(page, "进货订单统计列表")
    _web_row_count(page)
    # Purchase column filter is essential: exporting an unfiltered report is a
    # different dataset even if the workbook later happens to have few rows.
    for attempt in range(2):
        _click_exact(page, "筛选")
        try:
            _wait_dialog(page, "筛选", timeout_ms=20_000)
            break
        except CollectorError:
            if attempt:
                raise
    _select_unfinished_column(page)
    dialog = _visible_dialog(page, "筛选")
    lower_label = dialog.locator('label[id$="$edNumberValue1_label"]:visible')
    upper_label = dialog.locator('label[id$="$edNumberValue2_label"]:visible')
    if lower_label.count() != 1 or lower_label.inner_text().strip() != "≥" \
            or upper_label.count() != 1 or upper_label.inner_text().strip() != "≤":
        raise CollectorError("numeric_filter_operator_unverified")
    from_all = dialog.locator('input[id$="$rbAppend_radio0"]')
    if from_all.count() != 1:
        raise CollectorError("filter_scope_control_missing")
    from_all.check()
    if not from_all.is_checked():
        raise CollectorError("filter_scope_not_all")
    number = dialog.locator('input[id$="$edNumberValue1"]:visible')
    upper = dialog.locator('input[id$="$edNumberValue2"]:visible')
    if number.count() != 1 or upper.count() != 1 or upper.input_value().strip():
        raise CollectorError("numeric_filter_missing")
    number.fill(str(threshold))
    number.press("Tab")
    if number.input_value().strip() != str(threshold):
        raise CollectorError("numeric_filter_not_set")
    try:
        with page.expect_response(
            lambda response: _allowed_https(response.url)
            and urlsplit(response.url).path.endswith("/GetPagerSummary")
            and response.status == 200,
            timeout=45_000,
        ):
            _click_exact(page, "筛选", dialog="筛选")
    except Exception:
        raise CollectorError("filter_result_not_updated") from None
    try:
        _visible_dialog(page, "筛选")
    except CollectorError:
        pass
    else:
        raise CollectorError("filter_dialog_still_open")
    # The result can legitimately contain the same number of rows (for
    # example, when every row satisfies the threshold). The workbook
    # validator checks every exported value; a count difference is not a
    # reliable proof that the filter ran.
    return _stable_web_row_count(page)


class _DownloadProbe:
    """Bounded metadata-only trace covering every page in the browser context."""

    def __init__(self, context):
        self.events: list[dict[str, object]] = []
        self.downloads = []
        self.responses = []
        self.pages: set[int] = set()
        self.started = time.monotonic()
        context.on("page", self.watch_page)
        for page in context.pages:
            self.watch_page(page)

    def add(self, event: str, **fields) -> None:
        if len(self.events) < 400:
            self.events.append({"ms": round((time.monotonic() - self.started) * 1000),
                                "event": event, **fields})

    def watch_page(self, page) -> None:
        identity = id(page)
        if identity in self.pages:
            return
        self.pages.add(identity)
        self.add("page", page=identity)
        page.on("download", lambda item: self._download(identity, item))
        page.on("popup", lambda child: self.watch_page(child))
        page.on("dialog", lambda dialog: self.add("dialog", page=identity,
                                                    dialog_type=dialog.type))
        page.on("framenavigated", lambda frame: self._navigation(identity, frame))
        page.on("request", lambda request: self._request(identity, request))
        page.on("requestfailed", lambda request: self._request_failed(identity, request))
        page.on("response", lambda response: self._response(identity, response))

    def _download(self, identity, download) -> None:
        self.downloads.append(download)
        self.add("download", page=identity)

    def _navigation(self, identity, frame) -> None:
        url = frame.url
        if _allowed_https(url):
            parsed = urlsplit(url)
            self.add("navigation", page=identity, host=parsed.hostname,
                     path=parsed.path)

    def _request(self, identity, request) -> None:
        if _allowed_https(request.url):
            parsed = urlsplit(request.url)
            self.add("request", page=identity, host=parsed.hostname,
                     path=parsed.path, method=request.method,
                     resource_type=request.resource_type)

    def _request_failed(self, identity, request) -> None:
        if _allowed_https(request.url):
            parsed = urlsplit(request.url)
            self.add("request_failed", page=identity, host=parsed.hostname,
                     path=parsed.path, resource_type=request.resource_type)

    def _response(self, identity, response) -> None:
        if not _allowed_https(response.url):
            return
        parsed = urlsplit(response.url)
        content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
        request = response.request
        self.add("response", page=identity, host=parsed.hostname,
                 path=parsed.path, status=response.status,
                 resource_type=request.resource_type, content_type=content_type,
                 disposition_present="content-disposition" in response.headers)
        if ("spreadsheet" in content_type or "excel" in content_type or
                "octet-stream" in content_type or "content-disposition" in response.headers):
            self.responses.append(response)

    def save(self, path: Path) -> None:
        _private_dir(path.parent)
        temp = path.with_name("." + path.name + "." + uuid4().hex)
        temp.write_text(json.dumps({"schema": 1, "events": self.events},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, path)


def _download_report(page, destination: Path, *, diagnostics_file: Path | None = None,
                     inspect_only: bool = False) -> None:
    probe = _DownloadProbe(page.context)
    try:
        point = page.wait_for_function(
        """() => {
          const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);
            if(!(r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'))return false;
            const x=r.left+r.width/2,y=r.top+r.height/2;
            if(x<0||y<0||x>=innerWidth||y>=innerHeight)return false;
            const top=document.elementFromPoint(x,y);
            return !!top&&(top===el||el.contains(top));};
          const buttons=[...document.querySelectorAll('button[id$="$btnPrintNew"]')]
            .filter(visible).filter(el=>(el.innerText||'').trim()==='打印(F7)');
          if(buttons.length!==1)return null;
          const arrow=buttons[0].querySelector('.RightIcon.aicon-down');
          if(!arrow||!visible(arrow))return null;
          const a=arrow.getBoundingClientRect();
          return {x:a.left+a.width/2,y:a.top+a.height/2};
        }""", timeout=15_000,
        ).json_value()
    except Exception:
        raise CollectorError("print_dropdown_missing")
    try:
        page.mouse.click(point["x"], point["y"])
        probe.add("export_control", controls=page.evaluate("""() => {
          const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);
            return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&
              r.left<innerWidth&&r.top<innerHeight&&r.right>0&&r.bottom>0;};
          return [...document.querySelectorAll('button,input,a,span,div')]
            .filter(el=>visible(el)&&String(el.tagName==='INPUT'?el.value:el.innerText||'')
              .replace(/\\s+/g,' ').trim()==='导出数据')
            .slice(0,12).map(el=>{const r=el.getBoundingClientRect(),
              top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
              return {tag:el.tagName,id:String(el.id||'').slice(0,120),
                class_name:String(el.className||'').slice(0,120),
                parent_tag:el.parentElement?.tagName||'',
                parent_id:String(el.parentElement?.id||'').slice(0,120),
                hit_tag:top?.tagName||'',hit_id:String(top?.id||'').slice(0,120),
                has_inline_click:!!el.getAttribute('onclick')};});
        }"""))
        _click_exact(page, "导出数据")
        probe.add("after_export_click", controls=page.evaluate("""() => {
          return [...document.querySelectorAll('button,input,a,span,div')]
            .filter(el=>String(el.tagName==='INPUT'?el.value:el.innerText||'')
              .replace(/\\s+/g,' ').trim()==='导出数据')
            .slice(0,12).map(el=>{const r=el.getBoundingClientRect();
              const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
              const ancestors=[];for(let p=el.parentElement;p&&ancestors.length<4;p=p.parentElement)
                ancestors.push({tag:p.tagName,id:String(p.id||'').slice(0,120),
                  class_name:String(p.className||'').slice(0,120)});
              return {tag:el.tagName,id:String(el.id||'').slice(0,120),
                class_name:String(el.className||'').slice(0,120),
                visible:r.width>0&&r.height>0&&r.left<innerWidth&&r.top<innerHeight,
                hit_tag:hit?.tagName||'',hit_id:String(hit?.id||'').slice(0,120),
                ancestors};});
        }"""))
        if diagnostics_file is not None:
            probe.save(diagnostics_file)
        if inspect_only:
            raise CollectorError("export_ui_diagnosis_complete")
        # The menu item opens Beefun's PrintErrMsg confirmation, whose second
        # "导出数据" button actually starts the file transfer. This is distinct
        # from the menu item and must be scoped to the visible confirmation.
        confirmation = page.locator(".PrintErrMsg:visible button.aicon-daochu:visible")
        try:
            confirmation.wait_for(state="visible", timeout=10_000)
        except Exception:
            if not probe.downloads:
                raise CollectorError("export_confirmation_missing") from None
        else:
            if confirmation.count() != 1 or confirmation.inner_text().strip() != "导出数据":
                raise CollectorError("export_confirmation_ambiguous")
            probe.add("export_confirmation", count=1)
            confirmation.click()
        deadline = time.monotonic() + 120
        while not probe.downloads and time.monotonic() < deadline:
            page.wait_for_timeout(250)
        if not probe.downloads:
            raise CollectorError("download_event_missing_or_failed")
        if len(probe.downloads) != 1:
            raise CollectorError("multiple_downloads_ambiguous")
        download = probe.downloads[0]
        if download.failure():
            raise CollectorError("download_failed")
        download.save_as(destination)
    except CollectorError:
        raise
    except Exception:
        raise CollectorError("download_event_missing_or_failed") from None
    finally:
        if diagnostics_file is not None:
            probe.save(diagnostics_file)


def publish_snapshot(raw_file: Path, output_dir: Path, metadata: dict[str, object],
                     *, web_rows: int, account_scope: str) -> Path:
    """Publish immutable files, then atomically switch the latest pointer."""
    if not re.fullmatch(r"[0-9a-f]{64}", account_scope):
        raise CollectorError("account_scope_unverified")
    _private_dir(output_dir)
    snapshots = output_dir / "snapshots"
    _private_dir(snapshots)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_id = f"{stamp}-{str(metadata['sha256'])[:12]}-{uuid4().hex[:8]}"
    staged = snapshots / (".staging-" + snapshot_id)
    staged.mkdir(mode=0o700)
    try:
        workbook = staged / "purchase-unfinished.xlsx"
        shutil.copyfile(raw_file, workbook)
        workbook.chmod(0o600)
        if validate_export(workbook, start=date.fromisoformat(str(metadata["query_start"])),
                           end=date.fromisoformat(str(metadata["query_end"])),
                           min_unfinished=Decimal(str(metadata["filter"]).split(">=", 1)[1].strip()),
                           expected_rows=web_rows)["sha256"] != metadata["sha256"]:
            raise CollectorError("staged_hash_mismatch")
        manifest = {**metadata,
            "status": "validated", "source": "beefun_browser_export",
            "captured_at_utc": _now(), "web_row_count": web_rows,
            "account_scope": account_scope,
            "account_scope_basis": "login_company_and_verified_erp_host_v1",
            "snapshot_id": snapshot_id}
        (staged / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (staged / "manifest.json").chmod(0o600)
        final = snapshots / snapshot_id
        os.replace(staged, final)
        pointer = output_dir / (".latest-" + uuid4().hex + ".json")
        pointer.write_text(json.dumps({"snapshot_id": snapshot_id,
                                       "manifest": f"snapshots/{snapshot_id}/manifest.json"},
                                      ensure_ascii=False), encoding="utf-8")
        pointer.chmod(0o600)
        os.replace(pointer, output_dir / "latest.json")
        return final / "manifest.json"
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def collect(args) -> Path:
    from playwright.sync_api import sync_playwright

    credentials = _read_credentials(args.credentials_file)
    _private_dir(args.output_dir)
    temp_dir = args.output_dir / "staging"
    _private_dir(temp_dir)
    raw = temp_dir / ("download-" + uuid4().hex + ".xlsx")
    browser_args = {"headless": not args.headed}
    if args.chromium_executable:
        browser_args["executable_path"] = str(args.chromium_executable)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**browser_args)
            try:
                context = browser.new_context(accept_downloads=True,
                                              viewport={"width": 1920, "height": 930})
                page = context.new_page()
                page.set_default_timeout(15_000)
                page = _login(page, credentials, wait_for_human=args.wait_for_human,
                              status_paths=args.status_paths, started_at=args.started_at)
                account_scope = _account_scope(credentials["company"], page.url)
                web_rows = _open_filtered_report(page, start=args.start, end=args.end,
                                                 threshold=args.threshold)
                if web_rows == 0:
                    raise CollectorError("zero_rows_requires_manual_review")
                _download_report(page, raw, diagnostics_file=args.diagnostics_file,
                                 inspect_only=args.inspect_export)
                # Large all-history reports can still display the old pager
                # while the filtered export is being generated. Read the live
                # pager again after download; never derive it from file rows.
                web_rows = _stable_web_row_count(page)
                if web_rows == 0:
                    raise CollectorError("zero_rows_requires_manual_review")
            finally:
                browser.close()
        metadata = validate_export(raw, start=args.start, end=args.end,
                                   min_unfinished=args.threshold)
        if metadata["row_count"] != web_rows:
            if args.retain_rejected:
                quarantine = args.output_dir / "quarantine"
                _private_dir(quarantine)
                rejected = quarantine / ("row-count-mismatch-" + uuid4().hex + ".xlsx")
                shutil.copyfile(raw, rejected)
                rejected.chmod(0o600)
                compare = quarantine / (rejected.stem + ".json")
                compare.write_text(json.dumps({"reason": "web_file_count_mismatch",
                                               "web_row_count": web_rows,
                                               "file_row_count": metadata["row_count"],
                                               "minimum_unfinished": metadata["minimum_unfinished"],
                                               "required_minimum": str(args.threshold),
                                               "file_sha256": metadata["sha256"]},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
                compare.chmod(0o600)
            raise ExportValidationError("web_file_count_mismatch")
        return publish_snapshot(raw, args.output_dir, metadata, web_rows=web_rows,
                                account_scope=account_scope)
    finally:
        raw.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or collect a read-only unfinished purchase export")
    sub = parser.add_subparsers(dest="command", required=True)
    validation = sub.add_parser("validate", help="validate an existing Excel export, with no ERP login")
    live = sub.add_parser("collect", help="supervised Playwright collection; no schedule is installed")
    for command in (validation, live):
        command.add_argument("--start", type=date.fromisoformat, required=True)
        command.add_argument("--end", type=date.fromisoformat, required=True)
        command.add_argument("--threshold", type=Decimal, default=Decimal("0.1"))
    validation.add_argument("--xlsx", type=Path, required=True)
    validation.add_argument("--expected-rows", type=int)
    live.add_argument("--credentials-file", type=Path, required=True)
    live.add_argument("--output-dir", type=Path, required=True)
    live.add_argument("--chromium-executable", type=Path)
    live.add_argument("--headed", action="store_true")
    live.add_argument("--wait-for-human", action="store_true",
                      help="retain the headed browser on login verification; never resubmit automatically")
    live.add_argument("--diagnostics-file", type=Path)
    live.add_argument("--status-file", type=Path)
    live.add_argument("--inspect-export", action="store_true")
    live.add_argument("--retain-rejected", action="store_true")
    args = parser.parse_args(argv)
    started_at = _now()
    status_paths = ([args.output_dir / "collector-status.json"] +
                    ([args.status_file] if args.status_file else [])) if args.command == "collect" else []
    args.status_paths = status_paths
    args.started_at = started_at
    try:
        if args.command == "collect" and args.wait_for_human and not args.headed:
            raise CollectorError("human_wait_requires_headed_browser")
        if status_paths:
            _write_status(status_paths, "running", started_at)
        if args.command == "validate":
            result = validate_export(args.xlsx, start=args.start, end=args.end,
                                     min_unfinished=args.threshold, expected_rows=args.expected_rows)
        else:
            manifest = collect(args)
            result = {"status": "validated", "manifest": str(manifest)}
            _write_status(status_paths, "succeeded", started_at)
    except (CollectorError, ExportValidationError) as error:
        if status_paths:
            reason = str(error)
            state = "needs_human" if reason in {"manual_login_verification_required",
                                                  "login_form_missing_or_manual_verification",
                                                  "manual_login_session_closed"} else "failed"
            _write_status(status_paths, state, started_at, reason)
        print(json.dumps({"status": "failed", "reason": str(error),
                          "last_success_unchanged": True}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        if status_paths:
            _write_status(status_paths, "failed", started_at,
                          "unexpected_automation_error")
        print(json.dumps({"status": "failed", "reason": "unexpected_automation_error",
                          "last_success_unchanged": True}, ensure_ascii=False), file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
