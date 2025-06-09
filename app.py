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
# TMDB API Manager is no longer used for ID generation, but kept for reference if future meta needs arise.
class TmdbManager:
    """Manages interactions with TMDB API for metadata."""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.themoviedb.org/3"

    def search_movie_or_tv(self, title, year, item_type="movie"):
        """Searches TMDB for a movie or TV show by title and year."""
        if not self.api_key:
            logger.warning("TMDB_API_KEY is not set. Skipping TMDB search.")
            return None

        endpoint = f"{self.base_url}/search/{item_type}"
        params = {
            "api_key": self.api_key,
            "query": title,
            "year": year,
            "language": "en-US"
        }
        try:
            response = requests.get(endpoint, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            if data and data['results']:
                # Return the first result
                return data['results'][0]
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error searching TMDB for '{title}' ({year}): {e}")
            return None

    def get_external_ids(self, tmdb_id, item_type="movie"):
        """Fetches external IDs (like IMDb ID) for a given TMDB ID."""
        if not self.api_key:
            logger.warning("TMDB_API_KEY is not set. Cannot fetch external IDs.")
            return None

        endpoint = f"{self.base_url}/{item_type}/{tmdb_id}/external_ids"
        params = {"api_key": self.api_key}
        try:
            response = requests.get(endpoint, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching external IDs for TMDB ID '{tmdb_id}': {e}")
            return None

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
    # TMDB Manager is no longer passed as it's not used for ID generation
    def __init__(self, domain_resolver): # Removed tmdb_manager from __init__
        self.domain_resolver = domain_resolver
        # self.tmdb_manager = tmdb_manager # Removed

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

                    # Initialize other variables
                    title_base = "" 
                    year = ""
                    quality_details_raw = "" 
                    audio_languages = []
                    video_codec = ""
                    file_size = ""


                    # --- Logic for parsing title and quality ---
                    # Clean title_full first: remove common suffixes and redundant spaces/hyphens
                    # Also aggressively remove any leading/trailing brackets from the *full* title
                    cleaned_title_full = title_full.replace(" - ESub", "").replace(" -ESubs", "").strip()
                    cleaned_title_full = re.sub(r'^\s*\[|\]\s*$', '', cleaned_title_full).strip() # Remove leading/trailing brackets
                    cleaned_title_full = re.sub(r'[-\s]+$', '', cleaned_title_full).strip() # Remove trailing hyphens/spaces before final bracket split
                    
                    # Split title_full by the LAST '[' to separate base title from potential quality/extra info
                    title_parts_split_by_last_bracket = cleaned_title_full.rsplit('[', 1)
                    
                    # --- REFINED quality_details_raw extraction ---
                    soup = BeautifulSoup(description_html, 'html.parser')

                    # Priority 1: Extract from the torrent attachment link text - MOST RELIABLE
                    torrent_link_tag = soup.find('a', class_='ipsAttachLink', attrs={'data-fileext': 'torrent'})
                    if torrent_link_tag and torrent_link_tag.string:
                        torrent_filename_text = torrent_link_tag.string.strip()
                        # Extract the bracketed part from the torrent filename (e.g., "[720p HDRip -[Tamil + ...]]")
                        # This regex now correctly captures the part *before* the .torrent extension
                        quality_match_from_filename = re.search(r'\[([^\]]+?)(?:\s*-\s*ESubs?)?\](?=.*\.torrent)', torrent_filename_text, re.IGNORECASE)
                        if quality_match_from_filename:
                            # Keep the brackets for consistency in quality_details_raw
                            quality_details_raw = "[" + quality_match_from_filename.group(1).strip() + "]" 
                            logger.debug(f"Extracted quality_details_raw from torrent filename: {quality_details_raw}")
                    
                    # Fallback 2: Extract from the original title_full (if torrent link didn't yield results)
                    if not quality_details_raw and len(title_parts_split_by_last_bracket) > 1:
                        potential_raw_from_title = "[" + title_parts_split_by_last_bracket[1].rstrip(']').strip() + "]"
                        if potential_raw_from_title and potential_raw_from_title != "[]":
                            quality_details_raw = potential_raw_from_title
                            logger.debug(f"Extracted quality_details_raw from title_full (fallback): {quality_details_raw}")
                    
                    # Final cleanup of quality_details_raw - remove any lingering trailing hyphens or empty brackets
                    quality_details_raw = re.sub(r'[-\s]+$', '', quality_details_raw).strip()
                    quality_details_raw = re.sub(r'^\[\s*\]$', '', quality_details_raw).strip() # Remove "[]" if empty


                    # --- Continue title parsing ---
                    # Use the title parts from the original title_full split, but cleaned.
                    if len(title_parts_split_by_last_bracket) > 1:
                        title_candidate = title_parts_split_by_last_bracket[0].strip()
                        title_candidate = re.sub(r'[-\s]+$', '', title_candidate).strip() # Remove trailing hyphens/spaces
                    else:
                        title_candidate = cleaned_title_full.strip() # Use the fully cleaned title if no brackets
                        
                    # Extract year (if present) from title_candidate
                    year_match = re.search(r"\((\d{4})\)", title_candidate)
                    if year_match:
                        year = year_match.group(1)
                        # Remove the year and its parentheses from the title_candidate to get clean title_base
                        title_base = title_candidate.replace(year_match.group(0), '').strip()
                    else:
                        year = ""
                        title_base = title_candidate.strip()

                    # Clean up title_base from potential trailing "S01 EP(01-12)" or similar if it's not the year
                    episode_info_match = re.search(r'(S\d+E\d+|S\d+\s*EP\(\d+-\d+\)|Season \d+ Episode \d+)', title_base, re.IGNORECASE)
                    if episode_info_match:
                        title_base = title_base[:episode_info_match.start()].strip()
                        title_base = re.sub(r'[-\s]+$', '', title_base).strip() # Clean trailing hyphens/spaces again
                    
                    # FINAL cleaning of the title (item.name)
                    title = re.sub(r'[:\s-]+$', '', title_base).strip() # Remove any remaining trailing colons, spaces, hyphens
                    if title.endswith(' -'): # handle cases like "Movie Name -"
                        title = title[:-2].strip()

                    # Final decision for extracted_quality_for_name
                    # This section needs to produce a clean string without "Tamil + Telugu" if it's not a quality.
                    # It should prioritize resolutions, then HDRip/HD, then other meaningful quality indicators.
                    
                    temp_quality_for_extraction = quality_details_raw # Use the robustly extracted quality_details_raw
                    
                    # Extract languages directly from the *raw* quality string and then remove them for quality/codec parsing
                    language_match_in_quality = re.search(r'\[([^\]]+?)\]', temp_quality_for_extraction) # Non-greedy match for languages
                    if language_match_in_quality:
                        languages_str = language_match_in_quality.group(1).strip()
                        audio_languages = [lang.strip() for lang in re.split(r'\s*\+\s*', languages_str)]
                        # Now remove this language part from temp_quality_for_extraction for other details
                        temp_quality_for_extraction = temp_quality_for_extraction.replace(language_match_in_quality.group(0), '').strip()
                        temp_quality_for_extraction = re.sub(r'[-\s]+$', '', temp_quality_for_extraction).strip() # Clean trailing hyphens/spaces again
                    
                    # Remove surrounding brackets from temp_quality_for_extraction if they still exist
                    temp_quality_for_extraction = re.sub(r'^\s*\[|\]\s*$', '', temp_quality_for_extraction).strip()

                    concise_quality_elements = []
                    # Resolutions (4K, 1080p, 720p, 480p) - prioritize full word match
                    resolution_match = re.search(r'(\d+p|4K)', temp_quality_for_extraction, re.IGNORECASE)
                    if resolution_match:
                        concise_quality_elements.append(resolution_match.group(1).upper())
                    
                    # General quality terms (HD, HDRip)
                    if re.search(r'HDRip', temp_quality_for_extraction, re.IGNORECASE):
                        concise_quality_elements.append('HDRip')
                    elif re.search(r'HD', temp_quality_for_extraction, re.IGNORECASE):
                        concise_quality_elements.append('HD')
                    
                    # Video Codec (x264, H.264, H.265, HEVC, AVC)
                    video_codec_match = re.search(r'(x264|H\.264|H\.265|HEVC|AVC)', temp_quality_for_extraction, re.IGNORECASE)
                    if video_codec_match:
                        video_codec = video_codec_match.group(1).upper()
                    
                    # File Size (e.g., 16.5GB, 800MB)
                    size_match = re.search(r'(\d+(\.\d+)?(GB|MB))', temp_quality_for_extraction, re.IGNORECASE)
                    if size_match:
                        file_size = size_match.group(1).upper()

                    if concise_quality_elements:
                        extracted_quality_for_name = " ".join(concise_quality_elements)
                    elif temp_quality_for_extraction: 
                        # Fallback to first segment if no specific keywords, but ensure it's not just a codec
                        parts = [p.strip() for p in temp_quality_for_extraction.split('-') if p.strip()]
                        meaningful_parts = [p for p in parts if not re.match(r'(x\d+|H\.264|H\.265|HEVC|AVC|DD\d+\.\d+|AAC|AC3)', p, re.IGNORECASE)]
                        if meaningful_parts:
                            extracted_quality_for_name = meaningful_parts[0]
                        else:
                            extracted_quality_for_name = "Standard Quality" # If only non-quality parts remain
                    else:
                        extracted_quality_for_name = "Standard Quality" # More descriptive default

                    # description_quality should still reflect the full raw details for verbose description
                    description_quality = quality_details_raw if quality_details_raw else "No additional quality details available."

                    # --- Logic for parsing poster URL ---
                    # The soup object is already created at the beginning of the try block.
                    poster_url = ""

                    # Define known "bad" image patterns (emoticons, spacers, borders, small icons)
                    BAD_IMAGE_PATTERNS = [
                        r'spacer\.png$', 
                        r'emoticons/', # Catches all emoticons/gifs in that folder
                        r'torrborder\.gif$', 
                        r'torrenticon_92x92_blue\.png$',
                        r'92x92_blue\.png$', # More general if pattern varies
                        r'magnet-link-icon\.png$', # If other common icons appear
                        r'(\d+x\d+)\.(png|jpg|jpeg|gif)$' # Catches images with explicit small dimensions in filename, e.g. "92x92.png"
                    ]

                    # Function to check if an image tag is undesirable based on URL and optionally size
                    def is_undesirable_image(img_tag):
                        # Get URL from either data-src or src attribute
                        url = img_tag.get('data-src') or img_tag.get('src')
                        if not url:
                            return True # No valid URL to check

                        # 1. Always prioritize checking against known bad URL patterns
                        for pattern in BAD_IMAGE_PATTERNS:
                            if re.search(pattern, url, re.IGNORECASE):
                                logger.debug(f"Rejecting image by pattern: {url}")
                                return True # This image is undesirable

                        # 2. If not rejected by pattern, check dimensions if they are explicitly small
                        width = -1
                        height = -1
                        try:
                            width_str = img_tag.get('width')
                            if width_str is not None:
                                width = int(width_str)
                            height_str = img_tag.get('height')
                            if height_str is not None:
                                height = int(height_str)
                        except ValueError:
                            # If attributes are not valid integers, treat as unknown size and don't use this check
                            pass

                        # If both dimensions are available and are very small (e.g., icons, buttons)
                        if width > 0 and height > 0 and (width < 100 or height < 100): # Threshold for a "real" poster
                            logger.debug(f"Rejecting image by small explicit size: {url} ({width}x{height})")
                            return True

                        # If it passed all checks, it's considered a desirable image
                        return False

                    # Try to find a good poster with 'data-src' first, as suggested by XPath example
                    # This will use the most specific image as the primary poster candidate
                    img_candidates = soup.select('img[data-src]')
                    for img_tag in img_candidates:
                        if not is_undesirable_image(img_tag): # Check if it's not a bad image
                            poster_url = img_tag['data-src']
                            logger.debug(f"Selected poster URL (data-src, desirable): {poster_url}")
                            break
                    
                    # Fallback to 'src' if no suitable 'data-src' image was found
                    if not poster_url:
                        img_candidates_src = soup.select('img[src]')
                        for img_tag in img_candidates_src:
                            if not is_undesirable_image(img_tag): # Check if it's not a bad image
                                poster_url = img_tag['src']
                                logger.debug(f"Selected poster URL (src, desirable): {poster_url}")
                                break

                    poster_url = poster_url or "" # Ensure it's an empty string if nothing valid found
                    if not poster_url:
                         logger.debug(f"Final check: Poster still not found for '{title}'. Poster URL: '{poster_url}'")


                    magnet_link_tag = soup.find('a', class_='magnet-plugin', href=re.compile(r'magnet:\?xt=urn:btih:'))
                    magnet_uri = (magnet_link_tag['href'] if magnet_link_tag else None) or ""

                    # Construct stremio_id with custom prefix only
                    stremio_id = f"tamilshows:{re.sub(r'[^a-zA-Z0-9]', '', title).lower()}{year or ''}"
                    if not stremio_id: # Fallback if original title also becomes empty
                        stremio_id = f"tamilshows:unknown{entry.guid or int(time.time() * 1000)}"

                    # Parse pubDate to datetime object (after initial assignment)
                    try:
                        pub_date_dt = datetime.strptime(pub_date_str, '%a, %d %b %Y %H:%M:%S %z')
                    except ValueError:
                        try:
                            # Try parsing without timezone for systems where %z might not be supported or is missing
                            pub_date_dt = datetime.strptime(pub_date_str, '%a, %d %b %Y %H:%M:%S %Z')
                        except ValueError:
                            logger.warning(f"Could not parse pubDate '{pub_date_str}' for '{title_full}'. Using current time.")
                            # pub_date_dt already initialized, no need to re-assign unless different timezone is required
                            # Fallback to current time, attempting to preserve timezone if available from parsed_published
                            if entry.published_parsed:
                                try:
                                    pub_date_dt = datetime.now(entry.published_parsed.tzinfo)
                                except Exception:
                                    pub_date_dt = datetime.now()
                            else:
                                pub_date_dt = datetime.now()


                    # Add debug log for parsed information
                    logger.debug(f"Parsed Item: ID='{stremio_id}', Title='{title}', Year='{year}', "
                                 f"Quality_Concise='{extracted_quality_for_name}', Quality_Full='{description_quality}', "
                                 f"Audio_Languages='{audio_languages}', Video_Codec='{video_codec}', File_Size='{file_size}', "
                                 f"Poster='{poster_url}', Magnet='{magnet_uri}'")


                    if title and magnet_uri:
                        items_data.append({
                            'stremio_id': stremio_id,
                            'title': title,
                            'year': year,
                            'quality_details': description_quality, # Full details for description
                            'quality_for_name': extracted_quality_for_name, # Concise for stream name
                            'audio_languages': json.dumps(audio_languages), # Store as JSON string
                            'video_codec': video_codec,
                            'file_size': file_size,
                            'poster': poster_url,
                            'magnet_uri': magnet_uri,
                            'pub_date': pub_date_dt, 
                            'original_link': link
                        })
                except Exception as item_e:
                    logger.error(f"Error parsing RSS item: {item_e} (Title: {entry.get('title', 'N/A')}, PubDate String: {entry.get('published', 'N/A')})")
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
                "extraSupported": ["search"]
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
    Returns the catalog of Tamil shows.
    Stremio expects 'meta' objects for each item.
    """
    if type != "movie" or id != "tamil_shows_catalog": 
        return jsonify({"metas": []})

    all_items = redis_client.get_all_catalog_items()
    metas = []

    for item in all_items:
        if 'title' in item and 'poster' in item:
            meta = {
                "id": item['id'], # This will now be our custom 'tamilshows:' ID
                "type": "movie", 
                "name": item['title'], 
                "poster": item['poster'],
                "posterShape": "poster", # Changed from "regular" to "poster"
                "description": item.get('quality_details', 'No description available.'),
                "releaseInfo": item.get('year', ''),
                "genres": ["Tamil Shows", "Web Series"], 
                "runtime": ""
            }
            metas.append(meta)
    
    # Sort by publication date (most recent first)
    metas.sort(key=lambda x: x.get('pub_date', '0000-01-01T00:00:00Z'), reverse=True)

    logger.debug(f"Returning catalog with {len(metas)} items. Example meta: {json.dumps(metas[0] if metas else {}, indent=2)}")
    return jsonify({"metas": metas})


@app.route('/meta/<type>/<id>.json') # New /meta endpoint
def meta(type, id):
    """
    Returns full metadata for a given Stremio ID.
    This is requested by Stremio when a user clicks on a catalog item.
    """
    if type != "movie":
        return jsonify({"meta": None})

    item = redis_client.get_catalog_item(id) # ID here is the stremio_id

    if item:
        # Generate stream objects to embed directly
        streams_data = redis_client.get_streams_for_item(id)
        embedded_stremio_streams = []

        for s_data in streams_data:
            magnet_uri = s_data.get('magnet_uri')
            if not magnet_uri:
                logger.warning(f"No magnet URI found for stream '{id}'. Skipping stream entry in meta.")
                continue

            quality_for_name = s_data.get('quality_for_name') 
            description_quality = s_data.get('quality_details') or "No description available."
            audio_languages = json.loads(s_data.get('audio_languages', '[]'))
            video_codec = s_data.get('video_codec', '')
            file_size = s_data.get('file_size', '')
            
            final_magnet_uri = tracker_manager.append_trackers_to_magnet(magnet_uri)
            info_hash_match = re.search(r'btih:([^&]+)', final_magnet_uri)
            info_hash = info_hash_match.group(1) if info_hash_match else None

            if not info_hash:
                logger.warning(f"Could not extract infoHash for stream '{id}' with magnet URI: {magnet_uri}")
                continue
            
            tracker_urls_matches = re.findall(r'tr=([^&]+)', final_magnet_uri)
            stremio_sources = [f"tracker:{url}" for url in tracker_urls_matches]
            stremio_sources.append(f"dht:{info_hash}")

            # --- Constructing stream.name and stream.title ---
            stream_name_parts = ["TamilBlasters"]
            if quality_for_name and quality_for_name != "Standard Quality": # Changed condition
                stream_name_parts.append(quality_for_name)
            stream_name = " - ".join(stream_name_parts)

            stream_title_parts = [item.get('title', 'N/A')]
            if audio_languages:
                stream_title_parts.append(f"{', '.join(audio_languages)}")
            if video_codec:
                stream_title_parts.append(video_codec)
            if file_size:
                stream_title_parts.append(file_size)
            stream_title = " | ".join(filter(None, stream_title_parts))

            embedded_stremio_streams.append({
                "name": stream_name, 
                "description": stream_title, # Set description to be the same as stream_title
                "infoHash": info_hash,
                "sources": stremio_sources, 
                "title": stream_title
            })

        meta_obj = {
            "id": item['id'],
            "type": "movie",
            "name": item['title'], # Use the cleaned title from the item
            "poster": item['poster'],
            "posterShape": "poster", 
            "description": item.get('quality_details', 'No description available.'),
            "releaseInfo": item.get('year', ''),
            "genres": ["Tamil Shows", "Web Series"],
            "runtime": "",
            "streams": embedded_stremio_streams # Streams are now embedded here
        }
        logger.debug(f"Returning meta with embedded streams for '{id}': {json.dumps(meta_obj, indent=2)}")
        return jsonify({"meta": meta_obj})
    else:
        logger.warning(f"Metadata not found for ID: {id}")
        return jsonify({"meta": None})


@app.route('/stream/<type>/<stremio_id>.json')
def stream(type, stremio_id):
    """
    Returns stream links for a given Stremio ID.
    This endpoint is still implemented as per Stremio SDK recommendations,
    even though streams are now primarily embedded in the /meta response.
    It will be hit by Stremio if embedded streams are not present or failed for some reason.
    """
    if type != "movie": 
        return jsonify({"streams": []})

    streams_data = redis_client.get_streams_for_item(stremio_id)
    stremio_streams = []

    for s_data in streams_data:
        magnet_uri = s_data.get('magnet_uri')
        
        quality_for_name = s_data.get('quality_for_name') 
        description_quality = s_data.get('quality_details') or "No description available."
        
        audio_languages = json.loads(s_data.get('audio_languages', '[]'))
        video_codec = s_data.get('video_codec', '')
        file_size = s_data.get('file_size', '')
        
        if magnet_uri:
            final_magnet_uri = tracker_manager.append_trackers_to_magnet(magnet_uri)
            info_hash_match = re.search(r'btih:([^&]+)', final_magnet_uri)
            info_hash = info_hash_match.group(1) if info_hash_match else None

            if not info_hash:
                logger.warning(f"Could not extract infoHash for stream '{stremio_id}' with magnet URI: {magnet_uri}")
                continue
            
            tracker_urls_matches = re.findall(r'tr=([^&]+)', final_magnet_uri)
            stremio_sources = [f"tracker:{url}" for url in tracker_urls_matches]
            stremio_sources.append(f"dht:{info_hash}")

            # --- Constructing stream.name and stream.title ---
            stream_name_parts = ["TamilBlasters"]
            if quality_for_name and quality_for_name != "Standard Quality": # Changed condition
                stream_name_parts.append(quality_for_name)
            stream_name = " - ".join(stream_name_parts)

            # Use the original title from the item in Redis
            item_data = redis_client.get_catalog_item(stremio_id)
            original_title = item_data.get('title', 'N/A') if item_data else 'N/A'

            stream_title_parts = [original_title] 
            if audio_languages:
                stream_title_parts.append(f"{', '.join(audio_languages)}")
            if video_codec:
                stream_title_parts.append(video_codec)
            if file_size:
                stream_title_parts.append(file_size)
            stream_title = " | ".join(filter(None, stream_title_parts))

            stremio_stream = {
                "name": stream_name, 
                "description": stream_title, # Set description to be the same as stream_title
                "infoHash": info_hash,
                "sources": stremio_sources, 
                "title": stream_title
            }
            stremio_streams.append(stremio_stream)
    
    # Log the exact stream response JSON
    logger.debug(f"Returning stream response for '{stremio_id}': {json.dumps(stremio_streams, indent=2)}")

    # Sort streams by quality (e.g., 1080p before 720p)
    quality_order = {'4K': 5, '2160P': 4, '1080P': 3, '720P': 2, '480P': 1, 'STANDARD QUALITY': 0} # Added Standard Quality
    stremio_streams.sort(key=lambda x: quality_order.get(re.search(r'(\d+P|4K|STANDARD QUALITY)', x['name'].upper())[0] if re.search(r'(\d+P|4K|STANDARD QUALITY)', x['name'].upper()) else '', 0), reverse=True)


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
        quality_full_details = item['quality_details'] # Full string from brackets
        quality_concise_name = item['quality_for_name'] # Parsed for display
        audio_languages = item['audio_languages']
        video_codec = item['video_codec']
        file_size = item['file_size']
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
            'quality_details': quality_full_details, # Store full details
            'last_updated': datetime.now().isoformat(),
            'pub_date': pub_date.isoformat() if pub_date else datetime.now().isoformat()
        }

        if existing_catalog_item:
            # Check if a stream for this quality already exists for this item
            existing_streams = redis_client.get_streams_for_item(stremio_id)
            stream_exists = any(s.get('quality') == quality_concise_name for s in existing_streams)

            if not stream_exists:
                # Add new stream for existing content
                stream_data = {
                    'title': title,
                    'quality': quality_concise_name, # Store concise quality for lookup
                    'quality_details': quality_full_details, # Store full details for stream description
                    'audio_languages': audio_languages,
                    'video_codec': video_codec,
                    'file_size': file_size,
                    'magnet_uri': magnet_uri,
                    'pub_date': pub_date.isoformat() if pub_date else datetime.now().isoformat()
                }
                redis_client.add_stream_to_item(stremio_id, stream_data)
                updated_entries_count += 1
                logger.info(f"Added new quality stream for existing item: {title} ({quality_concise_name})")
            else:
                logger.debug(f"Item '{title}' with quality '{quality_concise_name}' already exists. Skipping update.")
        else:
            # New catalog item
            redis_client.set_catalog_item(stremio_id, catalog_data)
            
            # Add the first stream for this new item
            stream_data = {
                'title': title,
                'quality': quality_concise_name, # Store concise quality for lookup
                'quality_details': quality_full_details, # Store full details for stream description
                'audio_languages': audio_languages,
                'video_codec': video_codec,
                'file_size': file_size,
                'magnet_uri': magnet_uri,
                'pub_date': pub_date.isoformat() if pub_date else datetime.now().isoformat()
            }
            redis_client.add_stream_to_item(stremio_id, stream_data)
            new_entries_count += 1
            logger.info(f"Added new catalog item: {title} ({quality_concise_name})")

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
    # TMDB Manager is no longer used for ID generation
    # tmdb_manager = TmdbManager(TMDB_API_KEY) 
    tracker_manager = TrackerManager(TRACKERS_URL)
    # Pass domain_resolver only to RSS Parser
    rss_parser = RSSParser(domain_resolver) # Removed tmdb_manager from constructor

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
