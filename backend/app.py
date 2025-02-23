from flask import Flask, jsonify, request, render_template
from elasticsearch import Elasticsearch
import time
import os
try:
    # from .DataExtractor import connect_to_elasticsearch, scrape_rss_feed, upload_to_elasticsearch
    # from contentssummariser import summarize_text
    from .Translator import translate_text
    from .category import extract_articles_by_category
    from .search import search_articles
except ModuleNotFoundError as e:
    print(f"Error: Could not import module - {e}")
    print("Ensure all backend files are in the same directory as app.py")
    exit(1)

app = Flask(__name__)
ES_ENDPOINT = os.getenv("ELASTICSEARCH_ENDPOINT")
ES_USERNAME = os.getenv("ELASTICSEARCH_USERNAME")
ES_PASSWORD = os.getenv("ELASTICSEARCH_PASSWORD")
es = Elasticsearch(
    hosts=[ES_ENDPOINT],
    basic_auth=(ES_USERNAME, ES_PASSWORD),
)
CATEGORIES = ["Top", "Sports", "World", "States", "Cities", "Entertainment"]

RSS_FEEDS = {
    "Top": "https://feeds.feedburner.com/ndtvnews-top-stories",
    "Sports": "https://feeds.feedburner.com/ndtvsports-latest",
    "World": "https://feeds.feedburner.com/ndtvnews-world-news",
    "States": "https://feeds.feedburner.com/ndtvnews-south",
    "Cities": "https://feeds.feedburner.com/ndtvnews-cities-news",
    "Entertainment": "https://feeds.feedburner.com/ndtvmovies-latest"
}
@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200
@app.route('/')
def index():
    start_time = time.time()
    
    # Check for category or search query in the request
    category = request.args.get('category')
    search_query = request.args.get('search')
    
    if category:
        # Fetch articles by category
        query = {"query": {"match": {"category": category}}, "size": 1000}
    elif search_query:
        # Fetch articles by search query
        query = {"query": {"multi_match": {"query": search_query, "fields": ["title^3", "content"], "fuzziness": "AUTO"}}, "size": 1000}
    else:
        # Fetch all articles
        query = {"query": {"match_all": {}}, "size": 1000}
    
    response = es.search(index="news-articles", body=query)
    query_time = time.time() - start_time
    print(f"ES query took: {query_time:.3f} seconds")
    
    articles = [hit['_source'] for hit in response['hits']['hits']]
    print(f"Found {len(articles)} articles in database")
    
    render_start = time.time()
    response = render_template('index.html', articles=articles, categories=CATEGORIES)
    render_time = time.time() - render_start
    print(f"Rendering index page took: {render_time:.3f} seconds")
    
    total_time = time.time() - start_time
    print(f"Total time for index page: {total_time:.3f} seconds")
    return response

@app.route('/category/<category>', methods=['GET'])
def get_articles(category):
    start_time = time.time()
    articles = extract_articles_by_category(es, category)
    print(f"Category {category} fetch took: {time.time() - start_time:.3f} seconds")
    return jsonify(articles)

@app.route('/search', methods=['GET'])
def search_articles_endpoint():
    query = request.args.get('q', default='')
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    start_time = time.time()
    results = search_articles(es, query)
    articles = [hit['_source'] for hit in results]
    print(f"Search for '{query}' took: {time.time() - start_time:.3f} seconds")
    return jsonify(articles)

@app.route('/article_page/<path:title>')
def article_page(title):
    start_time = time.time()
    
    # Fetch the current article
    query = {"query": {"match": {"title": title}}}
    response = es.search(index="news-articles", body=query)
    query_time = time.time() - start_time
    print(f"ES query for '{title}' took: {query_time:.3f} seconds")
    
    if response['hits']['total']['value'] == 0:
        print(f"No article found for title: '{title}'")
        print(f"ES response: {response}")
        return "Article not found", 404
    
    article = response['hits']['hits'][0]['_source']
    print(f"Found article: {article}")
    
    # Fetch related articles based on content (more_like_this query)
    related_query = {
        "query": {
            "more_like_this": {
                "fields": ["title", "content"],
                "like": [
                    {
                        "_index": "news-articles",
                        "_id": response['hits']['hits'][0]['_id']
                    }
                ],
                "min_term_freq": 1,
                "max_query_terms": 12,
                "min_doc_freq": 1
            }
        }
    }
    related_response = es.search(index="news-articles", body=related_query, size=5)
    related_articles = [hit['_source'] for hit in related_response['hits']['hits'] if hit['_source']['title'] != title]
    
    summary_start = time.time()
    if 'summary' in article and article['summary']:
        print(f"Using precomputed summary for '{title}': {article['summary'][:50]}...")
    else:
        print(f"WARNING: No summary in ES for '{title}' (this should not happen with current dataextractor)")
        article['summary'] = "Summary missing unexpectedly."
    summary_time = time.time() - summary_start
    print(f"Summary retrieval took: {summary_time:.3f} seconds")

    lang = request.args.get('lang', default=None)
    if lang:
        translate_start = time.time()
        article['title'] = translate_text(article['title'], lang) or article['title']
        article['summary'] = translate_text(article['summary'], lang) or article['summary']
        translate_time = time.time() - translate_start
        print(f"Translation for '{title}' took: {translate_time:.3f} seconds")
    else:
        print(f"No translation requested for '{title}'")

    render_start = time.time()
    response = render_template('article.html', article=article, related_articles=related_articles, selected_lang=lang)
    render_time = time.time() - render_start
    print(f"Rendering template for '{title}' took: {render_time:.3f} seconds")
    
    total_time = time.time() - start_time
    print(f"Total time for /article_page/{title}: {total_time:.3f} seconds")
    return response

@app.route('/update', methods=['POST'])
def update_articles():
    start_time = time.time()
    new_articles = []
    for category, url in RSS_FEEDS.items():
        articles = scrape_rss_feed(url, category, es)  # Pass es for existence check
        new_articles.extend(articles)
    new_count = upload_to_elasticsearch(es, new_articles)
    print(f"Update process took: {time.time() - start_time:.3f} seconds")
    return jsonify({"message": f"Uploaded {new_count} new articles"})

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5001)
