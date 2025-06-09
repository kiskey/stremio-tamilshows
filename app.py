# app.py
import os
import re
import json
import logging
from flask import Flask, jsonify
from flask_cors import CORS # Import CORS
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
# TMDB API Key - No longer strictly needed for ID, but keeping for reference if future meta needs arise
TMDB_API_KEY = os.environ.get('TMDB_API_KEY', '') 
# Logging Level (e.g., 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
# Clear THIS APP's Redis cache on startup if 'true' or '1'
CLEAR_REDIS_ON_STARTUP = os.environ.get('CLEAR_REDIS_ON_STARTUP', 'false').lower() == 'true' # <<< Set to 'true' for one run after this update!

# --- Global Variables ---
app = Flask(__name__)
CORS(app) # Enable CORS for all routes and all origins
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
            if item:
                item['id'] = stremio_id # Add stremio_id to the item data
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

            # Check if a stream with the same infoHash already exists and update it
            updated = False
            for i, s in enumerate(existing_streams):
                if s.get('infoHash') == sanitized_stream_data.get('infoHash'):
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
        Handles both 'poster_thumbnail' and 'poster_medium' fields.
        This is for RSS-parsed posters, but kept for compatibility.
        """
        try:
            keys = self.client.keys("catalog:*")
            updated_count = 0
            for key in keys:
                item_data = self.client.hgetall(key)
                
                # Check and update poster_thumbnail (only if it's not a TMDb poster)
                if 'poster_thumbnail' in item_data and old_domain in item_data['poster_thumbnail'] and "image.tmdb.org" not in item_data['poster_thumbnail']:
                    new_poster_url = item_data['poster_thumbnail'].replace(old_domain, new_domain)
                    self.client.hset(key, 'poster_thumbnail', new_poster_url)
                    updated_count += 1
                    logger.info(f"Updated thumbnail poster domain for {item_data.get('id', 'N/A')}: {item_data['poster_thumbnail']} -> {new_poster_url}")
                
                # Check and update poster_medium (only if it's not a TMDb poster)
                if 'poster_medium' in item_data and old_domain in item_data['poster_medium'] and "image.tmdb.org" not in item_data['poster_medium']:
                    new_poster_url = item_data['poster_medium'].replace(old_domain, new_domain)
                    self.client.hset(key, 'poster_medium', new_poster_url)
                    updated_count += 1
                    logger.info(f"Updated medium poster domain for {item_data.get('id', 'N/A')}: {item_data['poster_medium']} -> {new_poster_url}")
            
            if updated_count > 0:
                logger.info(f"Updated domains for {updated_count} poster URLs from '{old_domain}' to '{new_domain}'.")
            else:
                logger.info(f"No non-TMDb poster URLs found to update for domain '{old_domain}'.")

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
            # Get all keys that start with "catalog:" or "streams:"
            catalog_keys = self.client.keys("catalog:*")
            stream_keys = self.client.keys("streams:*")
            
            all_app_keys = list(set(catalog_keys + stream_keys)) # Use set to avoid duplicates

            if all_app_keys:
                # Delete all identified keys in one go
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

# --- TMDB API Manager ---
class TmdbManager:
    """Manages interactions with TMDB API for metadata and images."""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.themoviedb.org/3"
        self.image_base_url = "https://image.tmdb.org/t/p/" # Default, will be updated by configuration
        self.poster_sizes = []
        self._get_configuration()

    def _get_configuration(self):
        """Fetches TMDb API configuration, including image base URL and sizes."""
        if not self.api_key:
            logger.warning("TMDB_API_KEY is not set. Cannot fetch TMDb configuration.")
            return

        config_url = f"{self.base_url}/configuration?api_key={self.api_key}"
        try:
            response = requests.get(config_url, timeout=5)
            response.raise_for_status()
            config = response.json()
            if 'images' in config:
                self.image_base_url = config['images']['secure_base_url'] + 't/p/'
                self.poster_sizes = config['images']['poster_sizes']
                logger.info(f"TMDb image configuration loaded. Base URL: {self.image_base_url}, Sizes: {self.poster_sizes}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching TMDb configuration: {e}")

    def get_image_url(self, file_path, size="w500"):
        """Constructs a full TMDb image URL."""
        if not file_path:
            return ""
        
        chosen_size = size
        if size not in self.poster_sizes and self.poster_sizes:
            # Fallback to a common size or 'original' if specific size not found
            if "w500" in self.poster_sizes:
                chosen_size = "w500"
            elif "original" in self.poster_sizes:
                chosen_size = "original"
            elif self.poster_sizes: # If any sizes exist, pick the first one
                chosen_size = self.poster_sizes[0]
            else: # No sizes at all, fallback to a hardcoded common size
                chosen_size = "w500" 
            logger.warning(f"Requested TMDb image size '{size}' not found. Using '{chosen_size}'.")
        
        return f"{self.image_base_url}{chosen_size}{file_path}"

    def search_movie_or_tv(self, title, year, language="en-US"):
        """
        Searches TMDb for a movie or TV show by title and year.
        Returns the first relevant result with a poster path and its type.
        """
        if not self.api_key:
            logger.warning("TMDB_API_KEY is not set. Skipping TMDb search.")
            return None, None

        # Try searching for TV series first
        tv_endpoint = f"{self.base_url}/search/tv"
        tv_params = {
            "api_key": self.api_key,
            "query": title,
            "first_air_date_year": year, # For TV series, use first_air_date_year
            "language": language
        }
        try:
            tv_response = requests.get(tv_endpoint, params=tv_params, timeout=5)
            tv_response.raise_for_status()
            tv_data = tv_response.json()
            if tv_data and tv_data['results']:
                for result in tv_data['results']:
                    if result.get('poster_path'):
                        logger.debug(f"Found TV poster for '{title}' on TMDb: {result['poster_path']}")
                        return result, "tv"
        except requests.exceptions.RequestException as e:
            logger.error(f"Error searching TMDb TV for '{title}' ({year}): {e}")

        # Fallback to searching for movies
        movie_endpoint = f"{self.base_url}/search/movie"
        movie_params = {
            "api_key": self.api_key,
            "query": title,
            "year": year, # For movies, use year
            "language": language
        }
        try:
            movie_response = requests.get(movie_endpoint, params=movie_params, timeout=5)
            movie_response.raise_for_status()
            movie_data = movie_response.json()
            if movie_data and movie_data['results']:
                for result in movie_data['results']:
                    if result.get('poster_path'):
                        logger.debug(f"Found Movie poster for '{title}' on TMDb: {result['poster_path']}")
                        return result, "movie"
        except requests.exceptions.RequestException as e:
            logger.error(f"Error searching TMDb Movie for '{title}' ({year}): {e}")

        logger.info(f"No suitable poster found on TMDb for '{title}' ({year}).")
        return None, None


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
        # Split only on the first '?' to correctly handle magnet URIs that might contain '?' in dn
        parts = magnet_uri.split('?', 1)
        base_magnet = parts[0]
        
        if len(parts) > 1:
            existing_params_str = parts[1]
            params = [p for p in existing_params_str.split('&') if not p.startswith('tr=')]
        else:
            params = [] # No existing parameters

        # Append new trackers
        for tracker in best_trackers:
            params.append(f"tr={tracker}")
        
        return f"{base_magnet}?{'&'.join(params)}" if params else base_magnet

# --- RSS Parser ---
class RSSParser:
    """Parses RSS feed items and extracts relevant data."""
    def __init__(self, domain_resolver, tmdb_manager): # Add tmdb_manager to constructor
        self.domain_resolver = domain_resolver
        self.tmdb_manager = tmdb_manager # Store tmdb_manager

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
                    
                    # --- Initialize pub_date_dt here ---
                    pub_date_dt = datetime.now() # Initialize with current datetime as a fallback

                    # Initialize other variables for stream-specific details
                    quality_details_raw = "" 
                    audio_languages = []
                    video_codec = ""
                    file_size = ""

                    # --- REFINED Logic for parsing concise title and year for catalog display ---
                    raw_entry_title = entry.title
                    
                    # Start with a working copy for base title extraction
                    working_title_for_base = raw_entry_title
                    
                    # 1. Extract year from anywhere in the title, and then remove it to simplify
                    # This targets (YYYY) format specifically, ensuring it's a word boundary
                    year_match = re.search(r"\b\((\d{4})\)\b", working_title_for_base) 
                    if year_match:
                        year = year_match.group(1)
                        # Remove the year and its parentheses from the working title
                        working_title_for_base = working_title_for_base.replace(year_match.group(0), '').strip()
                    else:
                        year = ""

                    # 2. Aggressively remove common quality/codec/size patterns in brackets or at the end
                    # Pattern for quality info in brackets: [RESOLUTION ... SIZE] or [LANGUAGES] or [CODEC] etc.
                    quality_pattern_in_brackets = r'\[\s*(?:(?:[A-Za-z0-9\s\+\-\.:\/]*?)(?:\d+p|4K|GB|MB|x\d+|DD\d+\.\d+|AC3|AAC|HDRip|WEB-DL|BluRay|BDRip|DVDRip|WebRip|AVC|HEVC|VP9|AV1|Dual Audio|Multi Audio|Org Audio|ESubs?)\s*[A-Za-z0-9\s\+\-\.:\/]*?)\s*\]'
                    working_title_for_base = re.sub(quality_pattern_in_brackets, '', working_title_for_base, flags=re.IGNORECASE).strip()
                    
                    # Pattern for quality info at the end, not necessarily in brackets, after " - "
                    quality_pattern_at_end = r'\s*-\s*(?:\d+p|4K|GB|MB|x\d+|DD\d+\.\d+|AC3|AAC|HDRip|WEB-DL|BluRay|BDRip|DVDRip|WebRip|AVC|HEVC|VP9|AV1|ESubs?).*$'
                    working_title_for_base = re.sub(quality_pattern_at_end, '', working_title_for_base, flags=re.IGNORECASE).strip()

                    # Remove any remaining trailing hyphens or spaces
                    working_title_for_base = re.sub(r'[-\s]+$', '', working_title_for_base).strip()
                    
                    # Remove leading/trailing empty brackets if they are still there
                    working_title_for_base = re.sub(r'^\s*\[|\]\s*$', '', working_title_for_base).strip()

                    # If after all cleaning, the title is empty, use a fallback
                    title = working_title_for_base or raw_entry_title.split('[')[0].strip() or "Unknown Title"
                    # Ensure no multiple spaces
                    title = re.sub(r'\s+', ' ', title).strip()

                    # Generate `base_stremio_id` (this will be the main ID for the catalog item)
                    # It's crucial this ID is consistent for all qualities of the same show
                    base_stremio_id = f"tamilshows:{re.sub(r'[^a-zA-Z0-9]', '', title).lower()}{year or ''}"
                    if not base_stremio_id or "unknowntitle" in base_stremio_id.lower(): 
                        base_stremio_id = f"tamilshows:unknown_item_{int(time.time() * 1000)}_{os.urandom(4).hex()}" # More unique fallback


                    # --- REFINED quality_details_raw extraction from description_html ---
                    # This is for the *stream's* description, should be full details.
                    soup_desc = BeautifulSoup(description_html, 'html.parser')
                    
                    torrent_link_tag = soup_desc.find('a', class_='ipsAttachLink', attrs={'data-fileext': 'torrent'})
                    if torrent_link_tag and torrent_link_tag.string:
                        torrent_filename_text = torrent_link_tag.string.strip()
                        quality_match_from_filename = re.search(r'\[([^\]]+?)(?:\s*-\s*ESubs?)?\](?=\.torrent)', torrent_filename_text, re.IGNORECASE)
                        if quality_match_from_filename:
                            quality_details_raw = quality_match_from_filename.group(1).strip()
                            logger.debug(f"Extracted quality_details_raw from torrent filename: [{quality_details_raw}]")
                    
                    if not quality_details_raw:
                        title_quality_match = re.search(r'\[([^\]]+?)(?:\s*-\s*ESubs?)?\]$', title_full, re.IGNORECASE)
                        if title_quality_match:
                            quality_details_raw = title_quality_match.group(1).strip()
                            logger.debug(f"Extracted quality_details_raw from title_full (fallback): [{quality_details_raw}]")

                    quality_details_raw = re.sub(r'\s*-\s*ESubs?$', '', quality_details_raw, flags=re.IGNORECASE).strip()
                    quality_details_raw = re.sub(r'^\s*\[|\]\s*$', '', quality_details_raw).strip() 
                    quality_details_raw = re.sub(r'[-\s]+$', '', quality_details_raw).strip() 
                    if quality_details_raw == "": 
                        quality_details_raw = "N/A"

                    # --- Parsing for audio_languages, video_codec, file_size from quality_details_raw ---
                    temp_quality_for_extraction = quality_details_raw 

                    language_match_in_quality = re.search(r'\[([^\]]+?)\]', temp_quality_for_extraction) 
                    if language_match_in_quality:
                        languages_str = language_match_in_quality.group(1).strip()
                        audio_languages = [lang.strip() for lang in re.split(r'\s*\+\s*', languages_str)]
                        temp_quality_for_extraction = temp_quality_for_extraction.replace(language_match_in_quality.group(0), '').strip()
                        temp_quality_for_extraction = re.sub(r'[-\s]+$', '', temp_quality_for_extraction).strip() 
                    
                    temp_quality_for_extraction = re.sub(r'^\s*\[|\]\s*$', '', temp_quality_for_extraction).strip()

                    video_codec_match = re.search(r'(x264|H\.264|H\.265|HEVC|AVC|VP9|AV1|DVDRip|WebRip|BRRip|BDRip|BluRay)', temp_quality_for_extraction, re.IGNORECASE)
                    if video_codec_match:
                        video_codec = video_codec_match.group(1).upper()
                    
                    size_match = re.search(r'(\d+(\.\d+)?(?:GB|MB))', temp_quality_for_extraction, re.IGNORECASE)
                    if size_match:
                        file_size = size_match.group(1).upper()

                    concise_quality_elements = []
                    resolution_match = re.search(r'(\d+p|4K)', temp_quality_for_extraction, re.IGNORECASE)
                    if resolution_match:
                        concise_quality_elements.append(resolution_match.group(1).upper())
                    
                    if re.search(r'HDRip', temp_quality_for_extraction, re.IGNORECASE):
                        concise_quality_elements.append('HDRip')
                    elif re.search(r'HD', temp_quality_for_extraction, re.IGNORECASE):
                        concise_quality_elements.append('HD')
                    elif re.search(r'WebDL|WEB-DL', temp_quality_for_extraction, re.IGNORECASE):
                        concise_quality_elements.append('WEB-DL')
                    elif re.search(r'BluRay|BDRip', temp_quality_for_extraction, re.IGNORECASE):
                        concise_quality_elements.append('BluRay')
                    
                    if concise_quality_elements:
                        extracted_quality_for_name = " ".join(concise_quality_elements)
                    elif temp_quality_for_extraction and temp_quality_for_extraction != "N/A": 
                        parts = [p.strip() for p in temp_quality_for_extraction.split('-') if p.strip()]
                        meaningful_parts = [p for p in parts if not re.match(r'(x\d+|H\.264|H\.265|HEVC|AVC|DD\d+\.\d+|AAC|AC3)', p, re.IGNORECASE)]
                        if meaningful_parts:
                            extracted_quality_for_name = meaningful_parts[0]
                        else:
                            extracted_quality_for_name = "Standard Quality"
                    else:
                        extracted_quality_for_name = "Standard Quality"

                    # Ensure all extracted values are strings
                    year = year or ""
                    quality_details_raw = quality_details_raw or "" 
                    video_codec = video_codec or ""
                    file_size = file_size or ""


                    # --- Poster URL Parsing (Original, as fallback) ---
                    # This logic will be superseded by TMDb if a poster is found there
                    catalog_poster_from_rss = ""
                    meta_poster_from_rss = ""

                    primary_img_tag = None
                    all_ips_images = soup_desc.find_all('img', class_='ipsImage', attrs={'data-src': True})

                    BAD_IMAGE_PATTERNS = [
                        r'spacer\.png$', 
                        r'emoticons/', 
                        r'torrborder\.gif$', 
                        r'torrenticon_92x92_blue\.png$',
                        r'92x92_blue\.png$', 
                        r'magnet-link-icon\.png$', 
                        r'(\d+x\d+)\.(png|jpg|jpeg|gif)$',
                        r'\.gif$'
                    ]

                    def is_undesirable_image(img_tag):
                        url = img_tag.get('data-src') or img_tag.get('src')
                        if not url:
                            return True
                        for pattern in BAD_IMAGE_PATTERNS:
                            if re.search(pattern, url, re.IGNORECASE):
                                return True
                        width = -1
                        height = -1
                        try:
                            width_str = img_tag.get('width')
                            if width_str is not None: width = int(width_str)
                            height_str = img_tag.get('height')
                            if height_str is not None: height = int(height_str)
                        except ValueError:
                            pass 
                        if width > 0 and height > 0 and (width < 100 or height < 100): 
                            return True
                        return False

                    for img_tag in all_ips_images:
                        if not is_undesirable_image(img_tag):
                            primary_img_tag = img_tag
                            break 

                    if primary_img_tag:
                        meta_poster_from_rss = primary_img_tag['data-src']
                        if '.md.jpg' in meta_poster_from_rss:
                            catalog_poster_from_rss = meta_poster_from_rss.replace('.md.jpg', '.th.jpg')
                        else:
                            catalog_poster_from_rss = meta_poster_from_rss
                    
                    catalog_poster_from_rss = catalog_poster_from_rss or ""
                    meta_poster_from_rss = meta_poster_from_rss or ""

                    # --- TMDb Poster Retrieval ---
                    tmdb_poster_thumbnail_url = ""
                    tmdb_poster_medium_url = ""
                    
                    if self.tmdb_manager.api_key: # Only try TMDb if API key is set
                        tmdb_result, tmdb_type = self.tmdb_manager.search_movie_or_tv(title, year)
                        if tmdb_result and tmdb_result.get('poster_path'):
                            poster_path = tmdb_result['poster_path']
                            tmdb_poster_thumbnail_url = self.tmdb_manager.get_image_url(poster_path, "w185")
                            tmdb_poster_medium_url = self.tmdb_manager.get_image_url(poster_path, "w500")

                    # Use TMDb posters if available, otherwise fall back to RSS-parsed ones
                    catalog_poster_url = tmdb_poster_thumbnail_url or catalog_poster_from_rss
                    meta_poster_url = tmdb_poster_medium_url or meta_poster_from_rss

                    # If all poster attempts fail, use a generic placeholder
                    if not catalog_poster_url:
                        catalog_poster_url = "https://placehold.co/185x278/000000/FFFFFF?text=No+Poster"
                        logger.warning(f"Using generic placeholder for catalog poster for '{title}'.")
                    if not meta_poster_url:
                        meta_poster_url = "https://placehold.co/500x750/000000/FFFFFF?text=No+Poster"
                        logger.warning(f"Using generic placeholder for meta poster for '{title}'.")


                    magnet_link_tag = soup_desc.find('a', class_='magnet-plugin', href=re.compile(r'magnet:\?xt=urn:btih:'))
                    magnet_uri = (magnet_link_tag['href'] if magnet_link_tag else None) or ""

                    # Parse pubDate to datetime object
                    try:
                        pub_date_dt = datetime.strptime(pub_date_str, '%a, %d %b %Y %H:%M:%S %z')
                    except ValueError:
                        try:
                            pub_date_dt = datetime.strptime(pub_date_str, '%a, %d %b %Y %H:%M:%S %Z')
                        except ValueError:
                            logger.warning(f"Could not parse pubDate '{pub_date_str}' for '{title_full}'. Using current time.")
                            if entry.published_parsed:
                                try:
                                    pub_date_dt = datetime.now(entry.published_parsed.tzinfo)
                                except Exception:
                                    pub_date_dt = datetime.now()
                            else:
                                pub_date_dt = datetime.now()


                    # Add debug log
                    logger.debug(f"Parsed Item: Base_ID='{base_stremio_id}', Concise_Title='{title}', Year='{year}', "
                                 f"Quality_Concise='{extracted_quality_for_name}', Quality_Full='{quality_details_raw}', "
                                 f"Audio_Languages='{audio_languages}', Video_Codec='{video_codec}', File_Size='{file_size}', "
                                 f"Poster_Thumbnail='{catalog_poster_url}', Poster_Medium='{meta_poster_url}', Magnet='{magnet_uri}'")


                    if title and magnet_uri:
                        items_data.append({
                            'base_stremio_id': base_stremio_id, # This is the unified ID for the catalog entry
                            'concise_title': title,           # This is the title for catalog display
                            'year': year,
                            'quality_details': quality_details_raw, # Full raw quality string for this specific stream
                            'quality_for_name': extracted_quality_for_name, # Concise quality for stream name
                            'audio_languages': json.dumps(audio_languages),
                            'video_codec': video_codec,
                            'file_size': file_size,
                            'poster_thumbnail': catalog_poster_url,
                            'poster_medium': meta_poster_url,
                            'magnet_uri': magnet_uri,
                            'pub_date': pub_date_dt, 
                            'original_link': link
                        })
                    else:
                        logger.warning(f"Skipping item due to missing title or magnet: Title='{title}', Magnet Present={bool(magnet_uri)}")
                except Exception as item_e:
                    logger.error(f"Error parsing RSS item: {item_e} (Title: {entry.get('title', 'N/A')}, PubDate String: {entry.get('published', 'N/A')})", exc_info=True)
            logger.info(f"Successfully parsed {len(items_data)} items from RSS feed.")
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch RSS feed from {feed_url}: {e}")
        except Exception as e:
            logger.error(f"An unexpected error occurred during RSS parsing: {e}", exc_info=True)
        
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
            "stream",
            "meta" # Added 'meta' resource
        ],
        "types": ["movie"], 
        "idPrefixes": ["tamilshows:"], # Added idPrefixes for our custom IDs
        "catalogs": [
            {
                "type": "movie", 
                "id": "tamil_shows_catalog",
                "name": "Tamil Shows",
                "extraRequired": [],
                "extraSupported": ["search", "skip"] # Added "skip" for pagination
            }
        ],
        "configurable": True,
        "detail": "Fetches content from 1TamilBlasters based on RSS feed. Automatically updates domain and trackers.",
        "icon": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e0/Stremio_Logo.png/800px-Stremio_Logo.png",
        "behaviorHints": {
            "configurable": True,
            "randomize": False,
            "p2p": True 
        }
    }
    return jsonify(manifest_data)

@app.route('/catalog/<type>/<id>.json')
@app.route('/catalog/<type>/<id>/<extra>.json')
def catalog(type, id, extra=None):
    """
    Returns the catalog of Tamil shows with pagination support.
    Stremio expects 'meta' objects for each item.
    """
    if type != "movie" or id != "tamil_shows_catalog": 
        return jsonify({"metas": []})

    skip = 0
    if extra:
        try:
            extra_dict = json.loads(extra)
            skip = int(extra_dict.get('skip', 0))
        except json.JSONDecodeError:
            logger.warning(f"Failed to decode extra parameter: {extra}. Assuming skip=0.")
            skip = 0
        except ValueError:
            logger.warning(f"Invalid skip value in extra parameter: {extra_dict.get('skip')}. Assuming skip=0.")
            skip = 0
    
    limit = 100 # Stremio's standard page size for catalogs

    all_items = redis_client.get_all_catalog_items()
    
    # Sort all items by publication date (most recent first) before applying pagination
    all_items.sort(key=lambda x: x.get('pub_date', '0000-01-01T00:00:00Z'), reverse=True)

    # Apply pagination by slicing the sorted list
    paginated_items = all_items[skip:skip + limit]

    metas = []
    for item in paginated_items: # Iterate over paginated items
        if 'title' in item and 'poster_thumbnail' in item: 
            meta = {
                "id": item['id'], # This is now the base_stremio_id
                "type": "movie", 
                "name": item['title'], # Use the concise title here
                "poster": item['poster_thumbnail'], # Use thumbnail for catalog
                "posterShape": "poster", 
                "description": item.get('description', 'No additional details available.'), # Use generic description for catalog
                "releaseInfo": item.get('year', ''),
                "genres": ["Tamil Shows", "Web Series"], 
                "runtime": ""
            }
            metas.append(meta)
    
    logger.debug(f"Returning catalog for skip={skip}, limit={limit} with {len(metas)} items. Example meta: {json.dumps(metas[0] if metas else {}, indent=2)}")
    return jsonify({"metas": metas})


@app.route('/meta/<type>/<id>.json') # New /meta endpoint
def meta(type, id):
    """
    Returns full metadata for a given Stremio ID.
    This is requested by Stremio when a user clicks on a catalog item.
    """
    if type != "movie":
        return jsonify({"meta": None})

    item = redis_client.get_catalog_item(id) # ID here is the base_stremio_id

    if item:
        # Retrieve ALL streams associated with this base_stremio_id
        streams_data = redis_client.get_streams_for_item(id)
        
        # Sort streams by quality (highest to lowest)
        quality_order = {'4K': 5, '2160P': 4, '1080P': 3, '720P': 2, '480P': 1, 'STANDARD QUALITY': 0}
        streams_data.sort(key=lambda x: quality_order.get(re.search(r'(\d+P|4K|STANDARD QUALITY)', x['quality'].upper())[0] if re.search(r'(\d+P|4K|STANDARD QUALITY)', x['quality'].upper()) else '', 0), reverse=True)

        embedded_stremio_streams = []

        for s_data in streams_data:
            magnet_uri = s_data.get('magnet_uri')
            if not magnet_uri:
                logger.warning(f"No magnet URI found for stream for base ID '{id}'. Skipping stream entry in meta.")
                continue

            quality_for_name = s_data.get('quality') # Use the stored concise quality
            description_quality = s_data.get('quality_details') or "No description available."
            audio_languages = json.loads(s_data.get('audio_languages', '[]'))
            video_codec = s_data.get('video_codec', '')
            file_size = s_data.get('file_size', '')
            
            final_magnet_uri = tracker_manager.append_trackers_to_magnet(magnet_uri)
            info_hash_match = re.search(r'btih:([^&]+)', final_magnet_uri)
            info_hash = info_hash_match.group(1) if info_hash_match else None

            if not info_hash:
                logger.warning(f"Could not extract infoHash for stream for base ID '{id}' with magnet URI: {magnet_uri}")
                continue
            
            tracker_urls_matches = re.findall(r'tr=([^&]+)', final_magnet_uri)
            stremio_sources = [f"tracker:{url}" for url in tracker_urls_matches]
            stremio_sources.append(f"dht:{info_hash}")

            # --- Constructing stream.name and stream.title for *individual stream* ---
            stream_name_parts = ["TamilBlasters"]
            if quality_for_name and quality_for_name != "Standard Quality":
                stream_name_parts.append(quality_for_name)
            stream_name = " - ".join(stream_name_parts)

            # Stream title should provide specific quality, language, codec, size
            stream_title_parts = [item.get('title', 'N/A')] # Base title from catalog item
            if audio_languages:
                stream_title_parts.append(f"[{', '.join(audio_languages)}]")
            if video_codec:
                stream_title_parts.append(video_codec)
            if file_size:
                stream_title_parts.append(file_size)
            stream_title = " | ".join(filter(None, stream_title_parts))


            embedded_stremio_streams.append({
                "name": stream_name, 
                "description": stream_title, 
                "infoHash": info_hash,
                "sources": stremio_sources, 
                "title": stream_title
            })

        meta_obj = {
            "id": item['id'],
            "type": "movie",
            "name": item['title'], # Use the concise title from the catalog item
            "poster": item['poster_medium'], 
            "posterShape": "poster", 
            "description": item.get('description', 'No additional details available.'), # Use the generic description for meta
            "releaseInfo": item.get('year', ''),
            "genres": ["Tamil Shows", "Web Series"],
            "runtime": "",
            "streams": embedded_stremio_streams # All sorted streams
        }
        logger.debug(f"Returning meta with embedded streams for '{id}': {json.dumps(meta_obj, indent=2)}")
        return jsonify({"meta": meta_obj})
    else:
        logger.warning(f"Metadata not found for ID: {id}")
        return jsonify({"meta": None})


# --- Scheduled Tasks ---

def update_rss_feed_and_catalog():
    """
    Scheduled task to fetch RSS feed, parse new items, and update Redis.
    Ensures unified catalog entries for same titles and groups streams.
    """
    logger.info("Starting scheduled RSS feed update and catalog refresh...")
    global current_rss_feed_url
    
    parsed_entries = rss_parser.parse_rss_feed(current_rss_feed_url)
    
    new_catalog_items_count = 0
    new_streams_added_count = 0

    # Process entries, grouping streams by base_stremio_id
    for entry_data in parsed_entries:
        base_stremio_id = entry_data['base_stremio_id']
        concise_title = entry_data['concise_title']
        item_year = entry_data['year']
        poster_thumbnail = entry_data['poster_thumbnail']
        poster_medium = entry_data['poster_medium']
        pub_date = entry_data['pub_date']

        # Prepare catalog data (general info about the show/movie)
        catalog_item_data = {
            'title': concise_title,
            'year': item_year,
            'poster_thumbnail': poster_thumbnail,
            'poster_medium': poster_medium,
            'description': f"{concise_title} ({item_year}) - Tamil Series/Movie. Click for available qualities." if item_year else f"{concise_title} - Tamil Series/Movie. Click for available qualities.", # More descriptive generic description
            'last_updated': datetime.now().isoformat(),
            'pub_date': pub_date.isoformat() if pub_date else datetime.now().isoformat()
        }

        # Check if the base catalog item already exists
        existing_catalog_item = redis_client.get_catalog_item(base_stremio_id)

        if not existing_catalog_item:
            # If the base catalog item doesn't exist, create it
            redis_client.set_catalog_item(base_stremio_id, catalog_item_data)
            new_catalog_items_count += 1
            logger.info(f"Created new catalog item: {concise_title} (ID: {base_stremio_id})")
        else:
            # If it exists, update relevant fields (like last_updated, pub_date if newer, or posters if missing)
            existing_catalog_item_pub_date_str = existing_catalog_item.get('pub_date', '0000-01-01T00:00:00Z')
            existing_catalog_item_pub_date = datetime.fromisoformat(existing_catalog_item_pub_date_str)
            
            # Update pub_date and last_updated if the current entry is newer
            if pub_date > existing_catalog_item_pub_date:
                redis_client.set_catalog_item(base_stremio_id, {
                    'pub_date': pub_date.isoformat(),
                    'last_updated': datetime.now().isoformat()
                })
            
            # Update posters if they were missing before, or if a better one appears (simple check)
            # Prioritize existing TMDb posters if present
            if not (existing_catalog_item.get('poster_thumbnail') and "image.tmdb.org" in existing_catalog_item['poster_thumbnail']) and poster_thumbnail:
                 redis_client.set_catalog_item(base_stremio_id, {'poster_thumbnail': poster_thumbnail})
            if not (existing_catalog_item.get('poster_medium') and "image.tmdb.org" in existing_catalog_item['poster_medium']) and poster_medium:
                 redis_client.set_catalog_item(base_stremio_id, {'poster_medium': poster_medium})


        # Now, add/update the specific stream data to this base catalog item's streams list
        # Extract infoHash for stream uniqueness check
        info_hash_match = re.search(r'btih:([^&]+)', entry_data['magnet_uri'])
        stream_info_hash = info_hash_match.group(1) if info_hash_match else None

        if not stream_info_hash:
            logger.warning(f"Skipping stream addition for '{concise_title}' due to missing infoHash in magnet URI: {entry_data['magnet_uri']}")
            continue # Skip adding this stream if infoHash is critical and missing

        stream_data = {
            'infoHash': stream_info_hash,
            'magnet_uri': entry_data['magnet_uri'],
            'quality': entry_data['quality_for_name'], # e.g., "1080P HDRip"
            'quality_details': entry_data['quality_details'], # e.g., "[1080p HDRip - [Tamil + ...]]"
            'audio_languages': entry_data['audio_languages'], # Already JSON string
            'video_codec': entry_data['video_codec'],
            'file_size': entry_data['file_size'],
            'pub_date': entry_data['pub_date'].isoformat() if entry_data['pub_date'] else datetime.now().isoformat()
        }
        
        # Check if this specific stream (by infoHash) already exists for this base_stremio_id
        existing_streams_for_base_id = redis_client.get_streams_for_item(base_stremio_id)
        stream_already_exists = any(s.get('infoHash') == stream_data['infoHash'] for s in existing_streams_for_base_id)

        if not stream_already_exists:
            redis_client.add_stream_to_item(base_stremio_id, stream_data)
            new_streams_added_count += 1
            logger.info(f"Added new stream for item '{concise_title}' (Quality: {entry_data['quality_for_name']})")
        else:
            logger.debug(f"Stream for item '{concise_title}' with quality '{entry_data['quality_for_name']}' and infoHash '{stream_info_hash}' already exists. Skipping.")

    logger.info(f"RSS feed update complete. New unique catalog items: {new_catalog_items_count}, New streams added: {new_streams_added_count}")


def scheduled_domain_resolution():
    """Scheduled task to resolve the current domain."""
    logger.info("Starting scheduled domain resolution...")
    global current_rss_feed_url, current_rss_feed_domain
    
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
        redis_client.clear_app_data() 

    # Initialize Managers
    domain_resolver = DomainResolver(MASTER_DOMAIN, redis_client)
    # Initialize TmdbManager (ensure TMDB_API_KEY is available in .env)
    tmdb_manager = TmdbManager(TMDB_API_KEY)
    tracker_manager = TrackerManager(TRACKERS_URL)
    # Pass tmdb_manager to RSS Parser
    rss_parser = RSSParser(domain_resolver, tmdb_manager) 

    # Perform initial setup tasks immediately on startup
    logger.info("Performing initial setup tasks...")
    
    # 1. Resolve initial domain and set current_rss_feed_url
    initial_resolved_url = domain_resolver.resolve_current_domain(RSS_FEED_URL_INITIAL)
    if initial_resolved_url:
        current_rss_feed_url = initial_resolved_url
    else:
        logger.error("Failed to resolve initial RSS feed URL. Exiting.")
        exit(1) 

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
