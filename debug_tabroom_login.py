"""
Standalone diagnostic -- NOT part of the FastAPI app.

Run locally (real network access to tabroom.com required):
    python3 debug_tabroom_login.py

What it does, in order:
  1. Fetches the real login page and prints every <form>'s action/method
     and every <input>'s name/type -- so we get the REAL field names
     instead of guessing again.
  2. Attempts login with your .env credentials using our current guessed
     field names, and prints the full response: status, all headers
     (especially Set-Cookie), and whether any cookie shows up at all.
  3. Fetches fields.mhtml?tourn_id=40342 (the real Loyola Invitational
     tourn_id we already confirmed) BOTH before and after the login
     attempt, and compares: row count, and whether "Jason Rong" /
     "Lynbrook" appears in the page at all. This tests directly whether
     the entries list differs for a logged-out vs logged-in request.
  4. Repeats steps 2-3 against staging.tabroom.com AND www.tabroom.com
     separately, since staging might be a fully separate account system
     from production -- if your credentials work on one but not the
     other, that's the bug, plain and simple.

Everything prints to the terminal AND saves raw HTML into ./tabroom_debug/
so you can open any of it in a browser if something looks off.
"""
import asyncio
import os
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

DEBUG_DIR = Path("tabroom_debug")
TOURN_ID = 40342  # confirmed real Loyola Invitational tourn_id from earlier


def dump_forms(html: str, label: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form")
    print(f"  Found {len(forms)} <form> element(s) on {label}:")
    for i, form in enumerate(forms):
        action = form.get("action")
        method = form.get("method", "get")
        inputs = form.find_all("input")
        # Only print forms that look login-relevant, to cut noise.
        input_names = [inp.get("name") for inp in inputs if inp.get("name")]
        relevant = any(
            n and ("user" in n.lower() or "pass" in n.lower() or "email" in n.lower())
            for n in input_names
        )
        if relevant or i == 0:
            print(f"    Form #{i}: action={action!r} method={method!r}")
            for inp in inputs:
                print(f"      <input type={inp.get('type')!r} name={inp.get('name')!r} value={inp.get('value')!r}>")


async def check_host(base_url: str, username: str, password: str) -> None:
    print(f"\n{'=' * 60}\n{base_url}\n{'=' * 60}")
    DEBUG_DIR.mkdir(exist_ok=True)
    host_label = base_url.replace("https://", "").replace(".", "_")

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        # --- Step 1: discover the real login form ---
        for path in ["/user/login/index.mhtml", "/user/login.mhtml", "/"]:
            resp = await client.get(f"{base_url}{path}")
            if resp.status_code == 200 and "login" in resp.text.lower():
                print(f"\n[1] Login page fetched from {path} (status {resp.status_code})")
                dump_forms(resp.text, path)
                (DEBUG_DIR / f"{host_label}_login_page.html").write_text(resp.text, encoding="utf-8")
                break
        else:
            print(f"\n[1] Couldn't find an obvious login page on {base_url}")

        # --- Step 2: fields.mhtml BEFORE login ---
        resp_before = await client.get(f"{base_url}/index/tourn/fields.mhtml", params={"tourn_id": TOURN_ID})
        has_jason_before = "Jason Rong" in resp_before.text
        row_count_before = resp_before.text.count('role="row"')
        print(f"\n[2] fields.mhtml BEFORE login: status={resp_before.status_code} "
              f"rows~={row_count_before} contains_jason_rong={has_jason_before}")
        (DEBUG_DIR / f"{host_label}_fields_before_login.html").write_text(resp_before.text, encoding="utf-8")

        # --- Step 3: attempt login with our current guessed field names ---
        login_resp = await client.post(
            f"{base_url}/user/login/login_save.mhtml",
            data={"username": username, "password": password},
        )
        print(f"\n[3] Login attempt: status={login_resp.status_code}")
        print(f"    Response URL after redirects: {login_resp.url}")
        print(f"    Cookies in jar after login attempt: {dict(client.cookies)}")
        print(f"    Response headers: {dict(login_resp.headers)}")
        body_snippet = login_resp.text[:500].replace("\n", " ")
        print(f"    Response body (first 500 chars): {body_snippet}")
        (DEBUG_DIR / f"{host_label}_login_response.html").write_text(login_resp.text, encoding="utf-8")

        # --- Step 4: fields.mhtml AFTER login attempt ---
        resp_after = await client.get(f"{base_url}/index/tourn/fields.mhtml", params={"tourn_id": TOURN_ID})
        has_jason_after = "Jason Rong" in resp_after.text
        row_count_after = resp_after.text.count('role="row"')
        print(f"\n[4] fields.mhtml AFTER login attempt: status={resp_after.status_code} "
              f"rows~={row_count_after} contains_jason_rong={has_jason_after}")
        (DEBUG_DIR / f"{host_label}_fields_after_login.html").write_text(resp_after.text, encoding="utf-8")

        if has_jason_before and has_jason_after:
            print("\n    -> 'Jason Rong' appears in fields.mhtml REGARDLESS of login. "
                  "Login isn't the issue -- the bug is in how find_entry_id() parses this page.")
        elif not has_jason_before and has_jason_after:
            print("\n    -> 'Jason Rong' ONLY appears after login. Confirms: this page needs "
                  "authentication, and our login is failing.")
        elif not has_jason_before and not has_jason_after:
            print("\n    -> 'Jason Rong' doesn't appear EITHER way. Either login truly failed "
                  "both times, or this page needs something login alone doesn't provide "
                  "(e.g. being a coach/judge on file for this specific tournament).")


async def main():
    username = os.environ["TABROOM_USERNAME"]
    password = os.environ["TABROOM_PASSWORD"]

    await check_host("https://staging.tabroom.com", username, password)
    await check_host("https://www.tabroom.com", username, password)

    print(f"\n\nDone. Full HTML saved in ./{DEBUG_DIR}/ if you want to dig further.")


if __name__ == "__main__":
    asyncio.run(main())
