from pathlib import Path, PureWindowsPath

from scripts.browser_probe import browser_candidates, discover_browser


def test_browser_override_is_authoritative_when_present(tmp_path: Path):
    browser = tmp_path / "chrome.exe"
    browser.write_bytes(b"fixture")
    assert discover_browser(environment={"CE_DECKY_BROWSER": str(browser)}, windows=True) == str(browser)


def test_windows_browser_candidates_include_installed_chrome_locations():
    candidates = browser_candidates(
        environment={
            "PROGRAMFILES": r"C:\Program Files",
            "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
            "LOCALAPPDATA": r"C:\Users\tester\AppData\Local",
        },
        windows=True,
    )
    windows_candidates = {PureWindowsPath(str(candidate)) for candidate in candidates if isinstance(candidate, Path)}
    assert PureWindowsPath(r"C:\Program Files\Google\Chrome\Application\chrome.exe") in windows_candidates
    assert PureWindowsPath(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe") in windows_candidates
    assert PureWindowsPath(r"C:\Users\tester\AppData\Local\Google\Chrome\Application\chrome.exe") in windows_candidates
