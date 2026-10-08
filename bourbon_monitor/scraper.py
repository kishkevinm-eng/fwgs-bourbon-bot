"""Product scraping from FWGS website"""

import logging
from datetime import datetime
from .browser import BrowserManager
from .config import Config, Constants

logger = logging.getLogger(__name__)


class ProductScraper:
    """Scrapes whiskey products from FWGS website"""

    def __init__(self, target_url=None, headless=True):
        self.target_url = target_url or Config.TARGET_URL
        self.headless = headless

    def scrape(self):
        """Scrape the website and return products"""
        with BrowserManager(headless=self.headless) as browser:
            browser.navigate_and_prepare(self.target_url)

            # Wait longer for JS to render products
            browser.page.wait_for_timeout(5000)

            # Try to wait for product cards
            try:
                browser.page.wait_for_selector('.card.vertical', timeout=10000)
            except Exception:
                logger.warning("Timeout waiting for .card.vertical selector")

            products = self._extract_products(browser)

            # Retry if too few products
            if len(products) < Constants.MIN_PRODUCTS_REASONABLE:
                logger.warning(f"Only found {len(products)} products, retrying...")
                browser.page.wait_for_timeout(5000)
                products = self._extract_products(browser)

            logger.info(f"Scrape complete: {len(products)} products found")
            return products

    def _extract_products(self, browser):
        """Extract products using JavaScript"""
        js_extract = """
        () => {
            const products = [];
            // Use the correct selector for this site
            const cards = document.querySelectorAll('.card.vertical');

            cards.forEach(card => {
                try {
                    // Get name from card__title
                    const titleEl = card.querySelector('.card__title, .card_title_name');
                    if (!titleEl) return;
                    const name = titleEl.textContent.trim();

                    // Get price from card__price-amount
                    const priceEl = card.querySelector('.card__price-amount');
                    if (!priceEl) return;
                    const priceText = priceEl.textContent.trim();
                    const priceMatch = priceText.match(/[\\d,]+\\.?\\d*/);
                    if (!priceMatch) return;
                    const price = parseFloat(priceMatch[0].replace(',', ''));

                    // Get availability from card__availability
                    const availEl = card.querySelector('.card__availability');
                    let availability = 0;
                    let status = 'available';

                    if (availEl) {
                        const availText = availEl.textContent.toLowerCase();
                        const stockMatch = availText.match(/(\\d+)\\s*(?:available|in stock)/);
                        if (stockMatch) {
                            availability = parseInt(stockMatch[1]);
                        } else if (availText.includes('in stock') || availText.includes('available')) {
                            availability = 1;
                        }

                        if (availText.includes('coming soon')) {
                            status = 'coming_soon';
                        } else if (availText.includes('lottery')) {
                            status = 'lottery';
                        } else if (availText.includes('out of stock') || availText.includes('sold out')) {
                            status = 'out_of_stock';
                        }
                    }

                    // Get URL from product-card-link
                    const link = card.querySelector('a.product-card-link, a[href*="/product/"]');
                    const url = link ? link.href : null;

                    products.push({ name, price, availability, status, url });
                } catch (e) {}
            });

            return products;
        }
        """

        try:
            products = browser.page.evaluate(js_extract)

            # Add timestamp
            for p in products:
                p['scraped_at'] = datetime.now().isoformat()

            logger.info(f"Extracted {len(products)} products")

            # Log each product
            for p in products:
                status_str = f" [{p['status']}]" if p['status'] != 'available' else ''
                logger.info(f"  - {p['name']} - ${p['price']} ({p['availability']} in stock){status_str}")

            return products

        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            return []
