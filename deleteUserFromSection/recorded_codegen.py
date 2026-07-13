import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://experience-admin.masaischool.com/")
    page.get_by_role("textbox", name="Your email").click()
    page.get_by_role("textbox", name="Your email").fill("ravi.kiran@masaischool.com")
    page.get_by_role("textbox", name="Your email").press("Tab")
    page.get_by_role("textbox", name="Your password").fill("m")
    page.get_by_role("textbox", name="Your password").press("ControlOrMeta+a")
    page.get_by_role("textbox", name="Your password").click()
    page.get_by_role("textbox", name="Your password").fill("mmAs@!4321")
    page.locator("svg").click()
    page.locator("body").press("ControlOrMeta+a")
    page.get_by_role("textbox", name="Your password").click()
    page.get_by_role("textbox", name="Your password").press("ControlOrMeta+a")
    page.get_by_role("textbox", name="Your password").fill("mAs@!4321")
    page.get_by_role("button", name="Sign In").click()
    page.get_by_role("link", name="Sections").click()
    page.get_by_role("cell", name="IITMDDSAI_2508_T3_ML301").click()
    page.get_by_role("button", name="Delete users").click()
    page.get_by_role("heading", name="delete User").click()
    page.get_by_text("student code").click()
    page.get_by_role("textbox", name="student code").click()
    page.get_by_role("textbox", name="student code").fill("iitp_2607_612")
    page.locator("div").filter(has_text=re.compile(r"^delete Userstudent codeiitp_2607_612$")).first.click()
    page.get_by_role("button", name="Delete", exact=True).click()
    page.get_by_text("Are you sure you want to").click()
    page.get_by_role("heading", name="Are you sure you want to").first.click()
    page.get_by_text("Are you sure you want to").click()
    page.get_by_role("button", name="Delete").click()
    page.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
