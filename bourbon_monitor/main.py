"""Main monitoring loop - OPTIMIZED FOR SPEED"""

import logging
import time
import signal
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from .config import Config, Constants, setup_logging
from .scraper import ProductScraper
from .notifier import DiscordNotifier
from .storage import ProductStorage

logger = logging.getLogger(__name__)

# Global flag for graceful shutdown
running = True


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    global running
    logger.info("Shutdown signal received, stopping...")
    running = False


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def run_check(storage: ProductStorage, notifier: DiscordNotifier, is_first_run: bool = False) -> bool:
    """
    Execute single monitoring check.
    
    Args:
        storage: ProductStorage instance (REUSED to maintain hot_items tracking)
        notifier: DiscordNotifier instance
        is_first_run: If True, establishes baseline without sending alerts
    
    Returns:
        True if check completed successfully, False otherwise
    """
    logger.info("=" * 60)
    logger.info(f"BOURBON MONITOR - {datetime.now().strftime('%B %d, %Y at %I:%M %p')}")
    logger.info("=" * 60)

    try:
        # Initialize scraper (new each time - browser needs fresh session)
        scraper = ProductScraper(Config.TARGET_URL, headless=Config.HEADLESS)

        # Load previous products
        old_products = storage.load()

        # Scrape current products
        
        with ThreadPoolExecutor(max_workers=1) as executor:
            new_products = executor.submit(scraper.scrape).result()


        # Safety check: if we found 0 products but had some before, skip this check
        if old_products and len(old_products) > 0 and len(new_products) == 0:
            logger.error(
                f"Found 0 products but was tracking {len(old_products)} - "
                "skipping check to prevent false alerts"
            )
            return False

        # Safety check: if we lost 50%+ of products, scrape likely failed
        if old_products and len(old_products) > 0:
            drop_percentage = (len(old_products) - len(new_products)) / len(old_products)
            if drop_percentage >= Constants.PRODUCT_DROP_THRESHOLD:
                logger.error(
                    f"Found {len(new_products)} products but was tracking "
                    f"{len(old_products)} ({drop_percentage*100:.0f}% drop) - "
                    "skipping check to prevent false alerts"
                )
                return False

        # Compare and detect changes
        new_arrivals = storage.get_new_products(old_products, new_products)

        # Detect status changes (coming_soon/lottery -> available)
        now_available = []
        if old_products and not is_first_run:
            old_by_name = {p["name"].lower(): p for p in old_products}
            for new_p in new_products:
                name_lower = new_p["name"].lower()
                old_p = old_by_name.get(name_lower)
                if old_p:
                    old_status = old_p.get("status", "available")
                    new_status = new_p.get("status", "available")
                    # If was coming_soon or lottery and now available
                    if old_status in ("coming_soon", "lottery") and new_status == "available":
                        now_available.append(new_p)
                        logger.info(f"STATUS CHANGE: {new_p['name']} went from {old_status} to available!")

        # Detect products that went out of stock
        out_of_stock = []

        # Track stock changes and detect hot items (only if not first run)
        # Returns only NEW hot items that haven't been notified yet
        hot_items_to_notify = []

        # Send notifications for new products (but not on first run)
        if new_arrivals and not is_first_run:
            # Fetch direct URLs for new products by clicking them
            notifier.send_new_products(new_arrivals)
            logger.info(f"NEW ARRIVALS: {len(new_arrivals)} product(s)")
        elif new_arrivals and is_first_run:
            logger.info(f"Establishing baseline with {len(new_arrivals)} product(s)")
        else:
            logger.info("No new products found")

        # Send notifications for products that became available
        if now_available:
            notifier.send_now_available(now_available)
            logger.info(f"NOW AVAILABLE: {len(now_available)} product(s) went live!")

        # Send notifications for hot items (only sent ONCE per item)
        if hot_items_to_notify:
            notifier.send_hot_items_dropping(hot_items_to_notify)
            logger.info(f"HOT ITEMS ALERT: {len(hot_items_to_notify)} product(s)")

        # Send notifications for items that went out of stock (regular notification)
        if out_of_stock:
            notifier.send_out_of_stock(out_of_stock)
            logger.info(f"OUT OF STOCK: {len(out_of_stock)} product(s)")

        # Save current state
        storage.save(new_products)

        logger.info("Check complete")
        return True

    except KeyboardInterrupt:
        raise

    except Exception as e:
        logger.error(f"Error during check: {e}", exc_info=True)

        error_str = str(e).lower()
        harmless_errors = ["browser has been closed", "target page", "context has been closed"]

        if not any(err in error_str for err in harmless_errors):
            try:
                notifier.send_error(str(e))
            except Exception as notify_error:
                logger.error(f"Failed to send error notification: {notify_error}")

        return False


def main():
    """Main monitoring loop"""
    setup_logging()

    logger.info("=" * 60)
    logger.info("BOURBON ONLINE EXCLUSIVES MONITOR")
    logger.info("=" * 60)
    logger.info(f"Target URL: {Config.TARGET_URL}")
    logger.info(f"Check Interval: {Config.CHECK_INTERVAL} minutes")
    logger.info(f"Headless Mode: {Config.HEADLESS}")
    logger.info(f"Log Directory: {Config.LOG_DIR}")
    logger.info(f"Data File: {Config.PRODUCTS_FILE}")
    logger.info("=" * 60)

    # Initialize components ONCE and reuse (maintains hot_items tracking!)
    storage = ProductStorage(Config.PRODUCTS_FILE)
    notifier = DiscordNotifier(Config.DISCORD_WEBHOOK_URL)

    # Run first check immediately (establish baseline)
    logger.info("Running initial check...")
    run_check(storage, notifier, is_first_run=True)

    # Send startup notification (throttled — skip if restarted within 30 min)
    try:
        import os, time
        startup_file = '/opt/bourbon-bot/data/.last_startup'
        should_notify = True
        if os.path.exists(startup_file):
            last_startup = os.path.getmtime(startup_file)
            if time.time() - last_startup < 1800:  # 30 minutes
                should_notify = False
                logger.info('Skipping startup notification (restarted within 30 min)')
        if should_notify:
            current_products = storage.load()
            notifier.send_startup(current_products)
        # Touch the file regardless
        with open(startup_file, 'w') as sf:
            sf.write(str(time.time()))
    except Exception as e:
        logger.warning(f"Could not send startup notification: {e}")

    # Main monitoring loop
    while running:
        try:
            wait_seconds = Config.CHECK_INTERVAL * 60
            logger.info(f"Next check in {Config.CHECK_INTERVAL} minutes...")

            # Sleep in 1-second intervals for responsive shutdown
            for _ in range(int(wait_seconds)):
                if not running:
                    break
                time.sleep(1)

            if not running:
                break

            # Run check (reuse storage to maintain hot_items tracking!)
            run_check(storage, notifier)

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
            break

        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            time.sleep(60)

    logger.info("Monitor stopped")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
