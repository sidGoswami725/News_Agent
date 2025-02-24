import feedparser
import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime
from elasticsearch import Elasticsearch
from contentssummariser import summarize_text

CHECK_INTERVAL = 60
ES_ENDPOINT = "https://9fb474a7f57d4bfbbd9e05246ff0b8ec.asia-south1.gcp.elastic-cloud.com:443"
ES_USERNAME = "elastic"
ES_PASSWORD = "6lWF4jG8mE5IUnOSc66kmSo1"
ES_INDEX = "news-articles"
MAX_TOKEN_LIMIT = 1024  # Max tokens for facebook/bart-large-cnn

def connect_to_elasticsearch():
    es = Elasticsearch(ES_ENDPOINT, basic_auth=(ES_USERNAME, ES_PASSWORD))
    return es

def scrape_article_content(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = soup.find_all('p')
        content = ' '.join(p.get_text(strip=True) for p in paragraphs)
        return content if content else None
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return None

def scrape_image_url(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        img_tags = soup.find_all('img')
        for img in img_tags:
            src = img.get('src') or img.get('data-src')
            if src and not any(keyword in src for keyword in ['scorecardresearch', 'ad', 'pixel', 'track', 'analytics']):
                return src
        return None
    except Exception as e:
        print(f"Error scraping image from {url}: {e}")
        return None

def estimate_token_count(text):
    # Rough estimate: 1 token ≈ 0.75 words
    words = text.split()
    return int(len(words) / 0.75)

def scrape_rss_feed(rss_url, category, es):
    feed = feedparser.parse(rss_url)
    articles = []
    for entry in feed.entries:
        link = entry.get('link', 'No Link')
        
        # Skip if article already exists in Elasticsearch
        if article_exists_in_elasticsearch(es, link):
            print(f"Skipping existing article: {entry.get('title', 'No Title')} (ID: {link})")
            continue
        
        content = scrape_article_content(link)
        if not content:
            print(f"Skipping {entry.get('title', 'No Title')} due to no content")
            continue
        
        # Check summarizer constraints
        token_count = estimate_token_count(content)
        if token_count > MAX_TOKEN_LIMIT:
            print(f"Skipping {entry.get('title', 'No Title')} - exceeds {MAX_TOKEN_LIMIT} tokens ({token_count})")
            continue
        if token_count < 50:  # Minimum reasonable length for summarization
            print(f"Skipping {entry.get('title', 'No Title')} - too short for summarization ({token_count} tokens)")
            continue
        
        # Generate summary at scrape time
        summary = summarize_text(content)
        if not summary or summary in ["No content available for summarization.", "Summarization failed due to an unexpected error.", "Summarization failed due to a persistent error.", "Empty summary generated from API"]:
            print(f"Skipping {entry.get('title', 'No Title')} - failed to generate valid summary")
            continue
        
        # Only proceed if summary is valid
        image_url = None
        if 'media_content' in entry and entry.media_content:
            for media in entry.media_content:
                if media.get('type', '').startswith('image/'):
                    image_url = media.get('url')
                    break
        if not image_url:
            image_url = scrape_image_url(link)
        
        article = {
            'title': entry.get('title', 'No Title'),
            'link': link,
            'published': entry.get('published', 'No Date'),
            'content': content,
            'summary': summary,
            'category': category,
            'image_url': image_url,
            'last_updated': datetime.now().isoformat()
        }
        articles.append(article)
        print(f"Added new article: {article['title']} - Summary: {article['summary'][:50]}... (Tokens: {token_count})")
    return articles

def article_exists_in_elasticsearch(es, link, index_name=ES_INDEX):
    try:
        return es.exists(index=index_name, id=link)
    except Exception as e:
        print(f"Error checking if article exists: {e}")
        return False

def upload_to_elasticsearch(es, articles, index_name=ES_INDEX):
    new_articles_uploaded = 0
    for article in articles:
        link = article['link']
        # Double-check existence to avoid race conditions
        if not article_exists_in_elasticsearch(es, link, index_name):
            response = es.index(index=index_name, id=link, document=article)
            print(f"Uploaded new article: {response['result']} (ID: {link})")
            new_articles_uploaded += 1
        else:
            if(article['summary'] in ["No content available for summarization.", "Summarization failed due to an unexpected error.", "Summarization failed due to a persistent error.", "Empty summary generated from API"]):
                es.delete(index=index_name, id=link)
            print(f"Article already exists (ID: {link}), skipping upload")
    return new_articles_uploaded

        
def check_bad_summary(es, index_name=ES_INDEX):
    # Define the list of bad summaries
    bad_summaries = [
        "No content available for summarization.",
        "Summarization failed due to an unexpected error.",
        "Summarization failed due to a persistent error.",
        "Empty summary generated from API"
    ]

    # Pagination parameters
    page_size = 100  # Number of documents to retrieve per page
    from_idx = 0  # Starting index for pagination

    while True:
        # Fetch a batch of documents
        response = es.search(
            index=index_name,
            body={
                "query": {
                    "match_all": {}  # Match all documents in the index
                },
                "from": from_idx,
                "size": page_size
            }
        )

        # Check if there are no more documents
        if not response['hits']['hits']:
            print("Finished checking all articles for bad summaries.")
            break

        # Process each document in the current batch
        for hit in response['hits']['hits']:
            doc_id = hit['_id']
            summary = hit['_source'].get('summary', '')

            # Check if the summary is in the list of bad summaries
            if summary in bad_summaries:
                print(f"Bad summary detected for article (ID: {doc_id}), deleting...")
                try:
                    es.delete(index=index_name, id=doc_id)
                    print(f"Deleted article with bad summary (ID: {doc_id})")
                except Elasticsearch.NotFoundError:
                    print(f"Article with bad summary not found (ID: {doc_id})")
                except Exception as e:
                    print(f"An error occurred while deleting article (ID: {doc_id}): {e}")

        # Move to the next batch
        from_idx += page_size
        
        
def main():
    rss_feeds = {
        "Top": "https://feeds.feedburner.com/ndtvnews-top-stories",
        "Sports": "https://feeds.feedburner.com/ndtvsports-latest",
        "World": "https://feeds.feedburner.com/ndtvnews-world-news",
        "States": "https://feeds.feedburner.com/ndtvnews-south",
        "Cities": "https://feeds.feedburner.com/ndtvnews-cities-news",
        "Entertainment": "https://feeds.feedburner.com/ndtvmovies-latest"
    }
    es = connect_to_elasticsearch()
    if not es.ping():
        print("❌ Could not connect to Elasticsearch.")
        return
    
    new_articles = []
    for category, url in rss_feeds.items():
        check_bad_summary(es)
        print(f"Scraping {category} feed...")
        articles = scrape_rss_feed(url, category, es)
        new_articles.extend(articles)
        print(f"Found {len(articles)} new articles in {category} feed.")
    if new_articles:
        new_articles_uploaded = upload_to_elasticsearch(es, new_articles)
        print(f"Uploaded {new_articles_uploaded} new articles.")
    else:
        print("No new articles found.")

if __name__ == "__main__":
    while True:
        print(f"Starting RSS feed check at {datetime.now().isoformat()}")
        main()
        print(f"Waiting for {CHECK_INTERVAL // 60} minute(s)...")
        time.sleep(CHECK_INTERVAL)

    
