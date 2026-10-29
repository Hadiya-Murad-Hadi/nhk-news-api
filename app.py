import re
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

app = Flask(__name__)

REGISTRATION = "FA23-BDS-012"
NEWS_SOURCE = "NHK World Japan"
SITEMAP_URL = "https://www3.nhk.or.jp/nhkworld/sitemap-news.xml"

HEADERS = {"User-Agent": "Mozilla/5.0"}


def get_sitemap_articles():
    response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=(4, 12))
    response.raise_for_status()
    root = ET.fromstring(response.content)

    articles = []
    for item in root:
        entry = {}
        for child in item:
            name = child.tag.split("}")[-1]
            if child.text:
                entry[name] = child.text.strip()

        url = entry.get("loc", "")
        if "/nhkworld/en/news/" in url:
            articles.append({
                "url": url,
                "title": entry.get("title", "")
            })

    return articles


def extract_article(url):
    response = requests.get(url, headers=HEADERS, timeout=(4, 8))
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    title_tag = soup.find("meta", property="og:title")
    title = (
        title_tag.get("content", "").strip()
        if title_tag
        else (soup.title.get_text(" ", strip=True) if soup.title else "")
    )

    article = soup.find("article") or soup.find("main") or soup.body
    if not article:
        raise ValueError("Article content was not found.")

    for element in article(["script", "style", "nav", "footer"]):
        element.decompose()

    text = re.sub(r"\s+", " ", article.get_text(" ", strip=True)).strip()
    if len(text) < 100:
        raise ValueError("The article text is too short.")

    return title, text


def summarize(text, keyword, max_sentences=3):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [
        sentence.strip()
        for sentence in sentences
        if len(sentence.strip().split()) >= 6
    ]

    if not sentences:
        return text[:600]

    keywords = set(re.findall(r"\w+", keyword.lower()))

    ranked = sorted(
        enumerate(sentences),
        key=lambda item: (
            len(set(re.findall(r"\w+", item[1].lower())) & keywords),
            -item[0]
        ),
        reverse=True
    )

    selected = sorted(ranked[:max_sentences], key=lambda item: item[0])
    return " ".join(sentence for _, sentence in selected)[:1500]


def verify_with_selenium(url):
    """Attempt to open the selected article using headless Chromium."""
    options = Options()
    options.binary_location = "/usr/bin/chromium"
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-http2")

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(8)
        driver.get(url)
        return driver.find_element("tag name", "body").text.strip()
    except Exception as exc:
        app.logger.warning("Selenium article check failed: %s", exc)
        return ""
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def get_first_article(keyword):
    articles = get_sitemap_articles()
    keyword_lower = keyword.lower()

    # Search titles first: this avoids fetching every article in the sitemap.
    title_matches = [
        article for article in articles
        if keyword_lower in article["title"].lower()
    ]

    # If no title matches, inspect only a small number of article bodies.
    candidates = title_matches if title_matches else articles[:5]
    last_error = None

    for item in candidates:
        url = item["url"]
        try:
            title, article_text = extract_article(url)
            combined = f"{title} {article_text}".lower()

            if keyword_lower not in combined:
                continue

            app.logger.info("Found matching article: %s", url)

            # Selenium opens the chosen article; requests provides a fallback.
            selenium_text = verify_with_selenium(url)
            if len(selenium_text) >= 100:
                article_text = selenium_text

            return url, summarize(article_text, keyword)

        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            app.logger.warning("Could not fetch %s: %s", url, exc)

    raise ValueError(
        f"No accessible NHK article matching '{keyword}' was found."
    ) from last_error


@app.route("/get", methods=["GET"])
def get_news():
    keyword = request.args.get("keyword", "").strip()

    if not keyword:
        return jsonify({
            "error": "Please provide a keyword, for example /get?keyword=technology"
        }), 400

    try:
        url, summary = get_first_article(keyword)
        return jsonify({
            "registration": REGISTRATION,
            "newssource": NEWS_SOURCE,
            "keyword": keyword,
            "url": url,
            "summary": summary
        })
    except Exception as error:
        app.logger.exception("News scraping failed")
        return jsonify({
            "registration": REGISTRATION,
            "newssource": NEWS_SOURCE,
            "keyword": keyword,
            "url": "",
            "summary": "Unable to retrieve the article: " + str(error)
        }), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7000)
