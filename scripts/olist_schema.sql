CREATE TABLE customers (
    customer_id TEXT NOT NULL PRIMARY KEY,
    customer_unique_id TEXT NOT NULL,
    customer_zip_code_prefix TEXT NOT NULL,
    customer_city TEXT NOT NULL,
    customer_state TEXT NOT NULL
);

CREATE TABLE geolocation (
    geolocation_id INTEGER PRIMARY KEY,
    geolocation_zip_code_prefix TEXT NOT NULL,
    geolocation_lat REAL NOT NULL,
    geolocation_lng REAL NOT NULL,
    geolocation_city TEXT NOT NULL,
    geolocation_state TEXT NOT NULL
);

CREATE TABLE category_translation (
    product_category_name TEXT NOT NULL PRIMARY KEY,
    product_category_name_english TEXT NOT NULL
);

CREATE TABLE products (
    product_id TEXT NOT NULL PRIMARY KEY,
    product_category_name TEXT,
    product_name_lenght INTEGER,
    product_description_lenght INTEGER,
    product_photos_qty INTEGER,
    product_weight_g INTEGER,
    product_length_cm INTEGER,
    product_height_cm INTEGER,
    product_width_cm INTEGER
);

CREATE TABLE sellers (
    seller_id TEXT NOT NULL PRIMARY KEY,
    seller_zip_code_prefix TEXT NOT NULL,
    seller_city TEXT NOT NULL,
    seller_state TEXT NOT NULL
);

CREATE TABLE orders (
    order_id TEXT NOT NULL PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    order_status TEXT NOT NULL,
    order_purchase_timestamp DATETIME NOT NULL,
    order_approved_at DATETIME,
    order_delivered_carrier_date DATETIME,
    order_delivered_customer_date DATETIME,
    order_estimated_delivery_date DATETIME NOT NULL
);

CREATE TABLE order_items (
    order_id TEXT NOT NULL REFERENCES orders (order_id),
    order_item_id INTEGER NOT NULL,
    product_id TEXT NOT NULL REFERENCES products (product_id),
    seller_id TEXT NOT NULL REFERENCES sellers (seller_id),
    shipping_limit_date DATETIME NOT NULL,
    price REAL NOT NULL,
    freight_value REAL NOT NULL,
    PRIMARY KEY (order_id, order_item_id)
);

CREATE TABLE order_payments (
    order_id TEXT NOT NULL REFERENCES orders (order_id),
    payment_sequential INTEGER NOT NULL,
    payment_type TEXT NOT NULL,
    payment_installments INTEGER NOT NULL,
    payment_value REAL NOT NULL,
    PRIMARY KEY (order_id, payment_sequential)
);

CREATE TABLE order_reviews (
    review_id TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders (order_id),
    review_score INTEGER NOT NULL,
    review_comment_title TEXT,
    review_comment_message TEXT,
    review_creation_date DATETIME NOT NULL,
    review_answer_timestamp DATETIME NOT NULL,
    PRIMARY KEY (review_id, order_id)
);

CREATE INDEX idx_customers_unique_id ON customers (customer_unique_id);
CREATE INDEX idx_customers_zip ON customers (customer_zip_code_prefix);
CREATE INDEX idx_geolocation_zip ON geolocation (geolocation_zip_code_prefix);
CREATE INDEX idx_products_category ON products (product_category_name);
CREATE INDEX idx_sellers_zip ON sellers (seller_zip_code_prefix);
CREATE INDEX idx_orders_customer ON orders (customer_id);
CREATE INDEX idx_orders_purchase_timestamp ON orders (order_purchase_timestamp);
CREATE INDEX idx_items_product ON order_items (product_id);
CREATE INDEX idx_items_seller ON order_items (seller_id);
CREATE INDEX idx_reviews_order ON order_reviews (order_id);

CREATE VIEW products_en AS
SELECT p.*, t.product_category_name_english
FROM products AS p
LEFT JOIN category_translation AS t USING (product_category_name);
