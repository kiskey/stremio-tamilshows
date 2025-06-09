# app.py
import os
import re
import json
import logging
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import time
import requests
import feedparser
from bs4 import BeautifulSoup
import redis

# --- Configuration from Environment Variables ---
# Initial RSS Feed URL - This will be updated dynamically
# Example: https://www.1tamilblasters.fi/index.php?/forums/forum/63-tamil-new-web-series-tv-shows.xml/
RSS_FEED_URL_INITIAL = os.environ.get('RSS_FEED_URL_INITIAL')
# Master Domain for redirection
MASTER_DOMAIN = os.environ.get('MASTER_DOMAIN', 'http://1tamilblasters.net/')
# Redis Configuration
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_DB = int(os.environ.get('REDIS_DB', 0))
# Scheduling Intervals (in hours)
FETCH_INTERVAL_HOURS = int(os.environ.get('FETCH_INTERVAL_HOURS', 3))
DOMAIN_CHECK_INTERVAL_HOURS = int(os.environ.get('DOMAIN_CHECK_INTERVAL_HOURS', 24))
# Data Retention: Entries older than this year will be removed
DELETE_OLDER_THAN_YEARS = int(os.environ.get('DELETE_OLDER_THAN_YEARS', 2))
# URL to fetch the latest torrent trackers
TRACKERS_URL = os.environ.get('TRACKERS_URL', 'https://ngosang.github.io/trackerslist/trackers_all.txt')
# Logging Level (e.g., 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
# Clear THIS APP's Redis cache on startup if 'true' or '1'
CLEAR_REDIS_ON_STARTUP = os.environ.get('CLEAR_REDIS_ON_STARTUP', 'false').lower() == 'true'

# --- Global Variables ---
app = Flask(__name__)
scheduler = BackgroundScheduler()
redis_client = None  # Initialized in main
current_rss_feed_domain = None
current_rss_feed_url = RSS_FEED_URL_INITIAL
best_trackers = []

# --- Logging Setup ---
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

# --- Helper Functions (Modularized below for better organization) ---

# --- Redis Manager ---
class RedisManager:
    """Manages interactions with the Redis database."""
    def __init__(self, host, port, db):
        self.client = redis.StrictRedis(host=host, port=port, db=db, decode_responses=True)
        logger.info(f"Connected to Redis at {host}:{port}/{db}")

    def _sanitize_data(self, data):
        """Converts any None values in a dictionary to empty strings for Redis compatibility."""
        if isinstance(data, dict):
            return {k: v if v is not None else "" for k, v in data.items()}
        return data if data is not None else ""

    def set_catalog_item(self, stremio_id, data):
        """Stores a catalog item in Redis."""
        try:
            sanitized_data = self._sanitize_data(data)
            self.client.hmset(f"catalog:{stremio_id}", sanitized_data)
            logger.info(f"Catalog item '{stremio_id}' stored/updated.")
        except Exception as e:
            logger.error(f"Error storing catalog item '{stremio_id}': {e}. Data: {data}")

    def get_catalog_item(self, stremio_id):
        """Retrieves a catalog item from Redis."""
        try:
            item = self.client.hgetall(f"catalog:{stremio_id}")
            return item if item else None
        except Exception as e:
            logger.error(f"Error retrieving catalog item '{stremio_id}': {e}")
            return None

    def add_stream_to_item(self, stremio_id, stream_data):
        """Adds a stream (or updates) for a catalog item in Redis."""
        try:
            # Store streams as a JSON string in a separate key
            # This allows multiple streams per item
            key = f"streams:{stremio_id}"
            # Fetch existing streams, append new, store back
            existing_streams_json = self.client.get(key)
            existing_streams = json.loads(existing_streams_json) if existing_streams_json else []

            sanitized_stream_data = self._sanitize_data(stream_data)

            # Check if a stream with the same quality already exists and update it
            updated = False
            for i, s in enumerate(existing_streams):
                if s.get('quality') == sanitized_stream_data.get('quality'):
                    existing_streams[i] = sanitized_stream_data
                    updated = True
                    break
            if not updated:
                existing_streams.append(sanitized_stream_data)

            self.client.set(key, json.dumps(existing_streams))
            logger.info(f"Stream for item '{stremio_id}' added/updated.")
        except Exception as e:
            logger.error(f"Error adding stream for item '{stremio_id}': {e}. Stream data: {stream_data}")

    def get_streams_for_item(self, stremio_id):
        """Retrieves streams for a catalog item from Redis."""
        try:
            streams_json = self.client.get(f"streams:{stremio_id}")
            return json.loads(streams_json) if streams_json else []
        except Exception as e:
            logger.error(f"Error retrieving streams for item '{stremio_id}': {e}")
            return []

    def get_all_catalog_items(self):
        """Retrieves all catalog items stored in Redis."""
        try:
            keys = self.client.keys("catalog:*")
            items = []
            for key in keys:
                stremio_id = key.replace("catalog:", "")
                item_data = self.client.hgetall(key)
                if item_data:
                    item_data['id'] = stremio_id # Add stremio_id to the item data
                    items.append(item_data)
            return items
        except Exception as e:
            logger.error(f"Error getting all catalog items: {e}")
            return []

    def delete_item(self, stremio_id):
        """Deletes a catalog item and its associated streams from Redis."""
        try:
            self.client.delete(f"catalog:{stremio_id}")
            self.client.delete(f"streams:{stremio_id}")
            logger.info(f"Deleted catalog item and streams for '{stremio_id}'.")
        except Exception as e:
            logger.error(f"Error deleting item '{stremio_id}': {e}")

    def update_poster_domains(self, old_domain, new_domain):
        """
        Updates poster URLs for all catalog items in Redis if their domain matches old_domain.
        """
        try:
            keys = self.client.keys("catalog:*")
            updated_count = 0
            for key in keys:
                item_data = self.client.hgetall(key)
                if item_data and 'poster' in item_data and old_domain in item_data['poster']:
                    new_poster_url = item_data['poster'].replace(old_domain, new_domain)
                    self.client.hset(key, 'poster', new_poster_url)
                    updated_count += 1
                    logger.info(f"Updated poster domain for {item_data.get('id', 'N/A')}: {item_data['poster']} -> {new_poster_url}")
            logger.info(f"Updated domains for {updated_count} poster URLs from '{old_domain}' to '{new_domain}'.")
        except Exception as e:
            logger.error(f"Error updating poster domains in Redis: {e}")

    def delete_older_entries(self, year_threshold):
        """
        Deletes catalog items older than the specified year_threshold.
        e.g., if year_threshold is 2, it deletes items older than (current_year - 2).
        """
        try:
            current_year = datetime.now().year
            threshold_year = current_year - year_threshold
            keys = self.client.keys("catalog:*")
            deleted_count = 0
            for key in keys:
                item_data = self.client.hgetall(key)
                if item_data and 'year' in item_data:
                    try:
                        item_year = int(item_data['year'])
                        if item_year < threshold_year:
                            stremio_id = key.replace("catalog:", "")
                            self.delete_item(stremio_id)
                            deleted_count += 1
                    except ValueError:
                        logger.warning(f"Could not parse year for item {key}: {item_data['year']}")
            logger.info(f"Cleaned up {deleted_count} entries older than {threshold_year}.")
        except Exception as e:
            logger.error(f"Error during old entry cleanup: {e}")

    def clear_app_data(self):
        """Clears only the data inserted by this application from the Redis database."""
        try:
            catalog_keys = self.client.keys("catalog:*")
            stream_keys = self.client.keys("streams:*")
            
            all_app_keys = list(set(catalog_keys + stream_keys)) # Use set to avoid duplicates

            if all_app_keys:
                self.client.delete(*all_app_keys)
                logger.info(f"Successfully cleared {len(all_app_keys)} application-specific keys from Redis database.")
            else:
                logger.info("No application-specific keys found in Redis to clear.")
        except Exception as e:
            logger.error(f"Error clearing application-specific data from Redis database: {e}")

# --- Domain Resolver ---
class DomainResolver:
    """Handles resolving the current domain of the RSS feed."""
    def __init__(self, master_domain, redis_manager):
        self.master_domain = master_domain
        self.redis_manager = redis_manager

    def resolve_current_domain(self, initial_rss_feed_url):
        """
        Attempts to resolve the current active domain for the RSS feed.
        First, checks if the initial_rss_feed_url is reachable.
        If not, or if no content, it tries to get the redirect from the master domain.
        Updates poster URLs in Redis if the domain changes.
        Returns the updated full RSS feed URL.
        """
        global current_rss_feed_domain
        current_domain_from_url = initial_rss_feed_url.split('/')[2] if initial_rss_feed_url else None
        
        # 1. Try to fetch the current RSS feed URL
        try:
            logger.info(f"Attempting to fetch RSS from current URL: {initial_rss_feed_url}")
            response = requests.get(initial_rss_feed_url, timeout=10)
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            if response.content:
                logger.info(f"Successfully fetched RSS from {initial_rss_feed_url}. Current domain seems active.")
                if current_rss_feed_domain != current_domain_from_url:
                    if current_rss_feed_domain:
                        logger.info(f"Domain potentially changed from {current_rss_feed_domain} to {current_domain_from_url}. Updating posters.")
                        self.redis_manager.update_poster_domains(current_rss_feed_domain, current_domain_from_url)
                    current_rss_feed_domain = current_domain_from_url
                return initial_rss_feed_url
            else:
                logger.warning(f"RSS feed from {initial_rss_feed_url} returned empty content. Trying master domain.")
        except (requests.exceptions.RequestException, requests.exceptions.HTTPError) as e:
            logger.warning(f"Failed to fetch RSS from {initial_rss_feed_url} ({e}). Trying master domain.")

        # 2. If the current RSS feed URL fails, try the master domain for redirection
        try:
            logger.info(f"Fetching master domain for redirection: {self.master_domain}")
            # Allow redirects and capture the final URL
            response = requests.get(self.master_domain, allow_redirects=True, timeout=10)
            response.raise_for_status()
            final_url = response.url
            logger.info(f"Master domain redirected to: {final_url}")

            # Extract the new domain from the final URL
            new_domain_parts = final_url.split('/')
            new_domain = f"{new_domain_parts[0]}//{new_domain_parts[2]}/" # e.g., https://www.newdomain.com/
            new_host_name = new_domain_parts[2] # e.g., www.newdomain.com

            logger.info(f"Resolved new base domain: {new_domain}")

            # If the domain has changed, update poster URLs in Redis
            if current_rss_feed_domain and current_rss_feed_domain != new_host_name:
                logger.info(f"Domain changed from {current_rss_feed_domain} to {new_host_name}. Updating poster URLs in Redis.")
                self.redis_manager.update_poster_domains(current_rss_feed_domain, new_host_name)
            
            current_rss_feed_domain = new_host_name

            # Construct the new RSS feed URL based on the new domain
            # We assume the path structure for the RSS feed remains consistent
            # e.g., if old was https://old.com/path/to/feed.xml
            # and new is https://new.com/, then new feed is https://new.com/path/to/feed.xml
            
            # Extract path from RSS_FEED_URL_INITIAL
            parsed_initial_url = requests.utils.urlparse(RSS_FEED_URL_INITIAL)
            rss_path = parsed_initial_url.path
            
            # Combine new base domain (with protocol) and old path
            new_rss_feed_url = f"{new_domain.rstrip('/')}{rss_path}"
            logger.info(f"Constructed new RSS Feed URL: {new_rss_feed_url}")
            
            return new_rss_feed_url

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to resolve current domain from master domain {self.master_domain}: {e}")
            return initial_rss_feed_url # Fallback to initial if resolution fails

# --- Tracker Manager ---
class TrackerManager:
    """Manages fetching and appending torrent trackers."""
    def __init__(self, trackers_url):
        self.trackers_url = trackers_url

    def fetch_trackers(self):
        """Fetches a list of best torrent trackers from the configured URL."""
        global best_trackers
        try:
            logger.info(f"Fetching trackers from: {self.trackers_url}")
            response = requests.get(self.trackers_url, timeout=10)
            response.raise_for_status()
            trackers_raw = response.text.strip().split('\n')
            # Filter out empty lines and ensure valid URL format (optional)
            trackers = [t.strip() for t in trackers_raw if t.strip() and t.strip().startswith(('udp://', 'http://'))]
            best_trackers = trackers
            logger.info(f"Successfully fetched {len(best_trackers)} trackers.")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching trackers from {self.trackers_url}: {e}. Using cached trackers if available.")

    def append_trackers_to_magnet(self, magnet_uri):
        """Appends fetched trackers to a given magnet URI."""
        if not best_trackers:
            logger.warning("No trackers available to append.")
            return magnet_uri

        # Remove existing 'tr' parameters to avoid duplicates
        base_magnet, existing_params_str = magnet_uri.split('?', 1)
        params = [p for p in existing_params_str.split('&') if not p.startswith('tr=')]
        
        # Append new trackers
        for tracker in best_trackers:
            params.append(f"tr={tracker}")
        
        return f"{base_magnet}?{'&'.join(params)}"

# --- RSS Parser ---
class RSSParser:
    """Parses RSS feed items and extracts relevant data."""
    def __init__(self, domain_resolver):
        self.domain_resolver = domain_resolver

    def parse_rss_feed(self, feed_url):
        """
        Fetches and parses the RSS feed from the given URL.
        Returns a list of dictionaries, each representing a parsed item.
        """
        items_data = []
        try:
            logger.info(f"Fetching RSS feed from: {feed_url}")
            response = requests.get(feed_url, timeout=20)
            response.raise_for_status()
            
            feed = feedparser.parse(response.text)

            if feed.bozo:
                logger.warning(f"RSS feed parsing error (bozo bit set): {feed.bozo_exception}")

            for entry in feed.entries:
                try:
                    title_full = entry.title
                    link = entry.link
                    description_html = entry.description
                    pub_date_str = entry.published
                    
                    # Initialize variables
                    title = ""
                    year = ""
                    quality_details = ""

                    # 1. Extract quality details from the last square brackets if present
                    quality_match = re.search(r"\[([^\[\]]+)\]$", title_full)
                    if quality_match:
                        quality_details = quality_match.group(1).strip()
                        # Remove the quality part from the full title for further parsing
                        remaining_title_for_year_title = title_full[:quality_match.start()].strip()
                    else:
                        remaining_title_for_year_title = title_full.strip() # No quality brackets found

                    # 2. Extract year from the remaining title string
                    year_match = re.search(r"\((\d{4})\)", remaining_title_for_year_title)
                    if year_match:
                        year = year_match.group(1)
                        # Remove the year part for the base title
                        title_before_year = remaining_title_for_year_title[:year_match.start()].strip()
                        title_after_year = remaining_title_for_year_title[year_match.end():].strip()
                        # Combine parts, ensuring spaces are handled
                        title = (title_before_year + (" " if title_before_year and title_after_year else "") + title_after_year).strip()
                    else:
                        title = remaining_title_for_year_title.strip() # No year found

                    # Ensure all extracted values are strings
                    title = title or ""
                    year = year or ""
                    quality_details = quality_details or ""

                    # Parse description for poster and magnet link
                    soup = BeautifulSoup(description_html, 'html.parser')
                    
                    poster_img = soup.find('img', class_='ipsImage', attrs={'data-src': True})
                    poster_url = (poster_img['data-src'] if poster_img else None) or ""

                    magnet_link_tag = soup.find('a', class_='magnet-plugin', href=re.compile(r'magnet:\?xt=urn:btih:'))
                    magnet_uri = (magnet_link_tag['href'] if magnet_link_tag else None) or ""

                    # Construct a unique ID for Stremio
                    stremio_id_base = re.sub(r'[^a-zA-Z0-9]', '', title).lower()
                    if not stremio_id_base: # Fallback if title becomes empty
                        stremio_id = f"tamilshows:unknown{entry.guid or int(time.time() * 1000)}"
                    else:
                        stremio_id = f"tamilshows:{stremio_id_base}{year or ''}"
                    
                    # Parse pubDate to datetime object
                    pub_date_dt = None
                    try:
                        pub_date_dt = datetime.strptime(pub_date_str, '%a, %d %b %Y %H:%M:%S %z')
                    except ValueError:
                        try:
                            pub_date_dt = datetime.strptime(pub_date_str, '%a, %d %b %Y %H:%M:%S %Z')
                        except ValueError:
                            logger.warning(f"Could not parse pubDate '{pub_date_str}' for '{title_full}'. Using current time.")
                            pub_date_dt = datetime.now(entry.published_parsed.tzinfo if entry.published_parsed else None) or datetime.now()

                    # Add debug log for parsed information
                    logger.debug(f"Parsed Item: ID='{stremio_id}', Title='{title}', Year='{year}', "
                                 f"Quality='{quality_details}', Poster='{poster_url}', Magnet='{magnet_uri}'")


                    if title and magnet_uri:
                        items_data.append({
                            'stremio_id': stremio_id,
                            'title': title,
                            'year': year,
                            'quality_details': quality_details,
                            'poster': poster_url,
                            'magnet_uri': magnet_uri,
                            'pub_date': pub_date_dt, # Store as datetime object temporarily for sorting, convert to string for Redis
                            'original_link': link
                        })
                except Exception as item_e:
                    logger.error(f"Error parsing RSS item: {item_e} (Title: {entry.get('title', 'N/A')})")
            logger.info(f"Successfully parsed {len(items_data)} items from RSS feed.")
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch RSS feed from {feed_url}: {e}")
        except Exception as e:
            logger.error(f"An unexpected error occurred during RSS parsing: {e}")
        
        return items_data

# --- Stremio Addon Endpoints ---

@app.route('/manifest.json')
def manifest():
    """Returns the Stremio addon manifest."""
    manifest_data = {
        "id": "org.stremio.tamilshows",
        "version": "1.0.0",
        "name": "Stremio TamilShows",
        "description": "Stremio addon to fetch Tamil web series and TV shows from 1TamilBlasters RSS feed.",
        "resources": [
            "catalog",
            "stream"
        ],
        "types": ["series"], # Assuming 'series' for TV shows/web series
        "catalogs": [
            {
                "type": "series",
                "id": "tamil_shows_catalog",
                "name": "Tamil Shows",
                "extraRequired": [],
                "extraSupported": ["search"]
            }
        ],
        "configurable": True,
        "detail": "Fetches content from 1TamilBlasters based on RSS feed. Automatically updates domain and trackers.",
        "icon": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e0/Stremio_Logo.png/800px-Stremio_Logo.png",
        "behaviorHints": {
            "configurable": True,
            "randomize": False
        }
    }
    return jsonify(manifest_data)

@app.route('/catalog/<type>/<id>.json')
@app.route('/catalog/<type>/<id>/<extra>.json')
def catalog(type, id, extra=None):
    """
    Returns the catalog of Tamil shows.
    Stremio expects 'meta' objects for each item.
    """
    if type != "series" or id != "tamil_shows_catalog":
        return jsonify({"metas": []})

    all_items = redis_client.get_all_catalog_items()
    metas = []

    for item in all_items:
        # Construct meta object suitable for Stremio
        if 'title' in item and 'poster' in item:
            meta = {
                "id": item['id'], # Use the stremio_id as the unique ID
                "type": "series", # Or 'movie' if the RSS feed contains movies
                "name": item['title'],
                "poster": item['poster'],
                "posterShape": "regular", # or 'landscape'
                "description": item.get('quality_details', 'No description available.'),
                "releaseInfo": item.get('year', ''),
                "genres": ["Tamil Shows", "Web Series"],
                "runtime": "" # Not available from RSS, but could be added if parsed
            }
            metas.append(meta)
    
    # Sort by publication date (most recent first)
    # Assuming 'pub_date' is stored as an ISO format string in Redis.
    metas.sort(key=lambda x: x.get('pub_date', '0000-01-01T00:00:00Z'), reverse=True)

    return jsonify({"metas": metas})


@app.route('/stream/<type>/<stremio_id>.json')
def stream(type, stremio_id):
    """
    Returns stream links for a given Stremio ID.
    """
    if type != "series": # Ensure type matches what we provide in catalog
        return jsonify({"streams": []})

    streams_data = redis_client.get_streams_for_item(stremio_id)
    stremio_streams = []

    for s_data in streams_data:
        magnet_uri = s_data.get('magnet_uri')
        quality = s_data.get('quality')

        if magnet_uri:
            # Append best trackers to the magnet URI
            final_magnet_uri = tracker_manager.append_trackers_to_magnet(magnet_uri)
            
            stremio_stream = {
                "name": f"Quality: {quality}",
                "description": f"Source: 1TamilBlasters - {quality}",
                "infoHash": re.search(r'btih:([^&]+)', final_magnet_uri).group(1) if re.search(r'btih:([^&]+)', final_magnet_uri) else None,
                "sources": [final_magnet_uri], # The final_magnet_uri already includes all best trackers
                "url": final_magnet_uri,
                "title": f"{s_data.get('title', 'N/A')} ({quality})"
            }
            stremio_streams.append(stremio_stream)
    
    # Sort streams by quality (e.g., 1080p before 720p)
    # This assumes quality strings are consistently parseable (e.g., '1080p', '720p', etc.)
    quality_order = {'2160p': 4, '1080p': 3, '720p': 2, '480p': 1}
    stremio_streams.sort(key=lambda x: quality_order.get(x['title'].split('(')[-1].strip(')').lower().replace(' ', '').replace('hdrip', 'p'), 0), reverse=True)


    return jsonify({"streams": stremio_streams})


# --- Scheduled Tasks ---

def update_rss_feed_and_catalog():
    """
    Scheduled task to fetch RSS feed, parse new items, and update Redis.
    """
    logger.info("Starting scheduled RSS feed update and catalog refresh...")
    global current_rss_feed_url
    
    items = rss_parser.parse_rss_feed(current_rss_feed_url)
    
    new_entries_count = 0
    updated_entries_count = 0

    for item in items:
        stremio_id = item['stremio_id']
        title = item['title']
        quality = item['quality_details']
        magnet_uri = item['magnet_uri']
        poster = item['poster']
        year = item['year']
        pub_date = item['pub_date']

        existing_catalog_item = redis_client.get_catalog_item(stremio_id)

        # Prepare item data for catalog (ensure values are strings)
        catalog_data = {
            'title': title,
            'year': year,
            'poster': poster,
            'quality_details': quality,
            'last_updated': datetime.now().isoformat(),
            'pub_date': pub_date.isoformat() if pub_date else datetime.now().isoformat()
        }

        if existing_catalog_item:
            # Check if a stream for this quality already exists for this item
            existing_streams = redis_client.get_streams_for_item(stremio_id)
            stream_exists = any(s.get('quality') == quality for s in existing_streams)

            if not stream_exists:
                # Add new stream for existing content
                stream_data = {
                    'title': title,
                    'quality': quality,
                    'magnet_uri': magnet_uri,
                    'pub_date': pub_date.isoformat() if pub_date else datetime.now().isoformat()
                }
                redis_client.add_stream_to_item(stremio_id, stream_data)
                updated_entries_count += 1
                logger.info(f"Added new quality stream for existing item: {title} ({quality})")
            else:
                logger.debug(f"Item '{title}' with quality '{quality}' already exists. Skipping update.")
        else:
            # New catalog item
            redis_client.set_catalog_item(stremio_id, catalog_data)
            
            # Add the first stream for this new item
            stream_data = {
                'title': title,
                'quality': quality,
                'magnet_uri': magnet_uri,
                'pub_date': pub_date.isoformat() if pub_date else datetime.now().isoformat()
            }
            redis_client.add_stream_to_item(stremio_id, stream_data)
            new_entries_count += 1
            logger.info(f"Added new catalog item: {title} ({quality})")

    logger.info(f"RSS feed update complete. New items: {new_entries_count}, Updated qualities: {updated_entries_count}")


def scheduled_domain_resolution():
    """Scheduled task to resolve the current domain."""
    logger.info("Starting scheduled domain resolution...")
    global current_rss_feed_url, current_rss_feed_domain
    
    # Get the current domain from the existing URL to pass as old_domain reference
    old_domain_from_url = requests.utils.urlparse(current_rss_feed_url).netloc
    
    new_resolved_url = domain_resolver.resolve_current_domain(current_rss_feed_url)
    
    if new_resolved_url and new_resolved_url != current_rss_feed_url:
        logger.info(f"Domain resolved: Old URL: {current_rss_feed_url}, New URL: {new_resolved_url}")
        current_rss_feed_url = new_resolved_url
    else:
        logger.info("Domain did not change or resolution failed.")


def scheduled_tracker_fetch():
    """Scheduled task to fetch the latest torrent trackers."""
    logger.info("Starting scheduled tracker fetch...")
    tracker_manager.fetch_trackers()


def scheduled_cleanup_old_entries():
    """Scheduled task to clean up old entries from Redis."""
    logger.info("Starting scheduled old entries cleanup...")
    redis_client.delete_older_entries(DELETE_OLDER_THAN_YEARS)


# --- Main Execution ---
if __name__ == '__main__':
    # Initialize Redis client
    redis_client = RedisManager(REDIS_HOST, REDIS_PORT, REDIS_DB)

    # Check if Redis should be cleared on startup
    if CLEAR_REDIS_ON_STARTUP:
        logger.info("CLEAR_REDIS_ON_STARTUP is true. Clearing application-specific Redis cache...")
        redis_client.clear_app_data() # Call the new method to clear only app data

    # Initialize Managers
    domain_resolver = DomainResolver(MASTER_DOMAIN, redis_client)
    tracker_manager = TrackerManager(TRACKERS_URL)
    rss_parser = RSSParser(domain_resolver)

    # Perform initial setup tasks immediately on startup
    logger.info("Performing initial setup tasks...")
    
    # 1. Resolve initial domain and set current_rss_feed_url
    initial_resolved_url = domain_resolver.resolve_current_domain(RSS_FEED_URL_INITIAL)
    if initial_resolved_url:
        current_rss_feed_url = initial_resolved_url
    else:
        logger.error("Failed to resolve initial RSS feed URL. Exiting.")
        exit(1) # Exit if we can't even get a starting URL

    # 2. Fetch initial trackers
    tracker_manager.fetch_trackers()
    
    # 3. Populate catalog for the first time
    update_rss_feed_and_catalog()

    # Schedule recurring tasks
    scheduler.add_job(update_rss_feed_and_catalog, 'interval', hours=FETCH_INTERVAL_HOURS, id='rss_fetch_job')
    scheduler.add_job(scheduled_domain_resolution, 'interval', hours=DOMAIN_CHECK_INTERVAL_HOURS, id='domain_resolve_job')
    scheduler.add_job(scheduled_tracker_fetch, 'interval', hours=24, id='tracker_fetch_job')
    scheduler.add_job(scheduled_cleanup_old_entries, 'interval', hours=24, id='cleanup_job')

    scheduler.start()
    logger.info("Scheduler started.")

    # Run Flask app
    logger.info("Stremio addon app starting...")
    app.run(host='0.0.0.0', port=8000)

    # Shutdown scheduler when app stops
    try:
        scheduler.shutdown()
    except Exception as e:
        logger.error(f"Error during scheduler shutdown: {e}")

