#!/usr/bin/env python3
import os
import sys
import json
import logging
import time
import subprocess
import threading
import signal
import redis
from datetime import datetime, timezone

# Add the directory containing the script to sys.path to allow importing notifier.py
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import notifier

# --- Configuration ---
SCRIPT_VERSION = "1.0.2"
LOG_DIR = "/var/log/health_checks"
LOG_FILE = os.path.join(LOG_DIR, "queue_processor.log")
REDIS_QUEUE_KEY = os.environ.get("REDIS_QUEUE_KEY", "health_checks:alert_queue")
PROCESSING_QUEUE_KEY = os.environ.get("PROCESSING_QUEUE_KEY", "health_checks:processing_queue")
REDIS_DEAD_LETTER_QUEUE_KEY = os.environ.get("REDIS_DEAD_LETTER_QUEUE_KEY", "health_checks:dead_letter_queue")
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))
RETRY_DELAY_SECONDS = float(os.environ.get("RETRY_DELAY_SECONDS", "1"))
PROCESSING_TIMEOUT = int(os.environ.get("PROCESSING_TIMEOUT", "300"))  # seconds
RECLAIM_INTERVAL = int(os.environ.get("RECLAIM_INTERVAL", "60"))  # seconds
WORKER_ID = f"pid:{os.getpid()}"

# --- Logging setup ---
if not os.path.isdir(LOG_DIR):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(process)d] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger(__name__)

# Graceful shutdown
shutdown_requested = False

def _signal_handler(signum, frame):
    global shutdown_requested
    logger.info(f"Signal {signum} received, shutting down gracefully...")
    shutdown_requested = True

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

# Helpers

def now_ts():
    return datetime.now(timezone.utc).isoformat()


def get_env_var(name: str, required: bool = True, default=None):
    value = os.environ.get(name)
    if required and value is None:
        logger.critical(f"Required environment variable {name} is not set. Exiting.")
        sys.exit(1)
    return value if value is not None else default


class RedisClient:
    _pool = None

    def __init__(self):
        if RedisClient._pool is None:
            redis_host = get_env_var('REDIS_HOST')
            redis_port = int(get_env_var('REDIS_PORT'))
            redis_password = get_env_var('REDISCLI_AUTH')
            RedisClient._pool = redis.ConnectionPool(
                host=redis_host, 
                port=redis_port, 
                username='default', 
                password=redis_password, 
                decode_responses=True,
                socket_timeout=30.0,
            )
        self.r = redis.Redis(connection_pool=RedisClient._pool)
        try:
            self.r.ping()
        except Exception as e:
            logger.critical(f"Failed to connect to Redis: {e}")
            raise


def _call_notifier(payload_obj: dict) -> int:
    """Retry a previously failed Telegram push. Returns 0 on success, 1 on failure.

    payload_obj schema: {"api_key": str, "payload": dict, "retries": int}
    where payload is already formatted (title/description/content) — notifier.py
    transforms and formats before enqueueing, so no re-transform is needed here.
    """
    try:
        payload = payload_obj.get('payload')
        api_key = payload_obj.get('api_key')
        if payload and api_key:
            success = notifier.push_to_api(api_key, payload)
            return 0 if success else 1
        return 1
    except Exception as e:
        logger.exception(f'Failed to call notifier logic: {e}')
        return 1


class Consumer:
    def __init__(self):
        self.redis = RedisClient().r
        self.reclaimer_thread = threading.Thread(target=self._reclaimer, daemon=True)

    def start(self):
        logger.info(f"Starting queue_processor consumer version {SCRIPT_VERSION}")
        self.reclaimer_thread.start()
        self._consume_loop()

    def _consume_loop(self):
        while not shutdown_requested:
            try:
                # BRPOPLPUSH atomically pops from main queue and pushes into processing queue
                item = self.redis.brpoplpush(REDIS_QUEUE_KEY, PROCESSING_QUEUE_KEY, timeout=5)
                if item is None:
                    continue

                original_item = item
                try:
                    obj = json.loads(item)
                except Exception:
                    logger.error(f"Received non-JSON item: {item}. Discarding.")
                    # remove the problematic item from processing queue
                    self.redis.lrem(PROCESSING_QUEUE_KEY, 1, item)
                    continue

                # annotate processing metadata by replacing the moved item with a modified copy
                obj.setdefault('retries', 0)
                obj['_processing_started'] = now_ts()
                obj['_processing_worker'] = WORKER_ID
                modified = json.dumps(obj)

                # find index of the moved item in processing queue and replace with modified
                replaced = False
                try:
                    queue_snapshot = self.redis.lrange(PROCESSING_QUEUE_KEY, 0, -1)
                    # find last occurrence of original_item
                    for idx in range(len(queue_snapshot)-1, -1, -1):
                        if queue_snapshot[idx] == original_item:
                            self.redis.lset(PROCESSING_QUEUE_KEY, idx, modified)
                            replaced = True
                            break
                except Exception:
                    logger.exception('Failed to set processing metadata in processing queue')

                if not replaced:
                    # best-effort: push modified and remove one occurrence of original
                    try:
                        self.redis.lpush(PROCESSING_QUEUE_KEY, modified)
                        self.redis.lrem(PROCESSING_QUEUE_KEY, 1, original_item)
                        replaced = True
                    except Exception:
                        logger.exception('Failed best-effort replace in processing queue')

                logger.info(f"Picked item for processing (retries={obj.get('retries', 0)})")

                # process
                returncode = _call_notifier(obj)

                if returncode == 0:
                    # success -> remove this processed item from processing_queue
                    try:
                        self.redis.lrem(PROCESSING_QUEUE_KEY, 1, modified)
                        logger.info('Processed and removed item from processing queue')
                    except Exception:
                        logger.exception('Failed to remove processed item from processing queue')
                else:
                    # failure -> increase retries, decide backoff or dead-letter
                    try:
                        obj['retries'] = int(obj.get('retries', 0)) + 1
                    except Exception:
                        obj['retries'] = 1
                    obj.pop('_processing_started', None)
                    obj.pop('_processing_worker', None)
                    if obj['retries'] > MAX_RETRIES:
                        obj['_moved_to_dead_at'] = now_ts()
                        self.redis.rpush(REDIS_DEAD_LETTER_QUEUE_KEY, json.dumps(obj))
                        # remove from processing queue
                        self.redis.lrem(PROCESSING_QUEUE_KEY, 1, modified)
                        logger.error('Max retries exceeded: moved item to dead-letter queue')
                    else:
                        # apply a simple backoff before requeueing to avoid tight loop
                        delay = RETRY_DELAY_SECONDS * (2 ** (obj['retries'] - 1))
                        delay = min(delay, 300)
                        logger.warning(f'Re-enqueueing item with retries={obj["retries"]} after {delay}s backoff')
                        time.sleep(delay)
                        self.redis.rpush(REDIS_QUEUE_KEY, json.dumps(obj))
                        self.redis.lrem(PROCESSING_QUEUE_KEY, 1, modified)
            except redis.exceptions.TimeoutError:
                # Normal empty-queue timeout on blocking pop, continue
                continue
            except redis.exceptions.ConnectionError:
                logger.exception('Redis connection error, sleeping before retry')
                time.sleep(5)
            except Exception:
                logger.exception('Unexpected error in consume loop, continuing')
                time.sleep(1)

        logger.info('Shutdown requested, exiting consume loop')

    def _reclaimer(self):
        """Periodically scan processing queue and reclaim stuck items."""
        while not shutdown_requested:
            try:
                snapshot = self.redis.lrange(PROCESSING_QUEUE_KEY, 0, -1)
                now = datetime.now(timezone.utc)
                for item in snapshot:
                    try:
                        obj = json.loads(item)
                    except Exception:
                        # non-json - skip
                        continue
                    started = obj.get('_processing_started')
                    if not started:
                        # if no timestamp, treat as stale after PROCESSING_TIMEOUT
                        # set a synthetic age by skipping this one iteration; will be reclaimed later
                        continue
                    try:
                        started_dt = datetime.fromisoformat(started)
                    except Exception:
                        continue
                    age = (now - started_dt).total_seconds()
                    if age > PROCESSING_TIMEOUT:
                        logger.warning(f'Reclaiming stuck item (age={age}s, retries={obj.get("retries",0)})')
                        # remove this item from processing_queue
                        try:
                            self.redis.lrem(PROCESSING_QUEUE_KEY, 1, item)
                        except Exception:
                            logger.exception('Failed to remove stuck item from processing queue')
                            continue
                        # increase retries and either requeue or dead-letter
                        try:
                            obj['retries'] = int(obj.get('retries', 0)) + 1
                        except Exception:
                            obj['retries'] = 1
                        obj.pop('_processing_started', None)
                        obj.pop('_processing_worker', None)
                        if obj['retries'] > MAX_RETRIES:
                            obj['_moved_to_dead_at'] = now_ts()
                            self.redis.rpush(REDIS_DEAD_LETTER_QUEUE_KEY, json.dumps(obj))
                            logger.error('Reclaimed item exceeded max retries: moved to dead-letter')
                        else:
                            self.redis.rpush(REDIS_QUEUE_KEY, json.dumps(obj))
                            logger.info('Reclaimed item requeued for retry')
                time.sleep(RECLAIM_INTERVAL)
            except redis.exceptions.ConnectionError:
                logger.exception('Redis connection error in reclaimer')
                time.sleep(5)
            except Exception:
                logger.exception('Unexpected error in reclaimer')
                time.sleep(5)


def main():
    # Ensure required envs are present (systemd EnvironmentFile expected to provide them)
    get_env_var('REDIS_HOST')
    get_env_var('REDIS_PORT')
    get_env_var('REDISCLI_AUTH')
    # NOTIFY API keys are used by notifier.py; consumer passes env through

    consumer = Consumer()
    try:
        consumer.start()
    except KeyboardInterrupt:
        logger.info('KeyboardInterrupt received, exiting')


if __name__ == '__main__':
    main()
