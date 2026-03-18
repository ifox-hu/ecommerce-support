SET NAMES utf8mb4;
CREATE DATABASE IF NOT EXISTS ecommerce_analytics CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
USE ecommerce_analytics;

-- BEGIN SHARED FIXTURE
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    category VARCHAR(40) NOT NULL,
    name VARCHAR(80) NOT NULL
);
CREATE TABLE channels (
    channel_id INTEGER PRIMARY KEY,
    name VARCHAR(80) NOT NULL,
    region VARCHAR(40) NOT NULL
);
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    order_date DATE NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    channel_id INTEGER NOT NULL REFERENCES channels(channel_id),
    status VARCHAR(20) NOT NULL
);
CREATE TABLE payments (
    order_id INTEGER PRIMARY KEY REFERENCES orders(order_id),
    amount DECIMAL(12, 2) NOT NULL,
    status VARCHAR(20) NOT NULL
);
CREATE TABLE refunds (
    refund_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(order_id),
    amount DECIMAL(12, 2) NOT NULL,
    status VARCHAR(20) NOT NULL
);
CREATE INDEX ix_analytics_orders_date ON orders (order_date);
CREATE INDEX ix_analytics_orders_channel ON orders (channel_id);
CREATE INDEX ix_analytics_orders_product ON orders (product_id);
CREATE INDEX ix_analytics_refunds_order_status ON refunds (order_id, status);

INSERT INTO products (product_id, category, name) VALUES
    (1, '鞋服', '轻便跑鞋'), (2, '数码', '无线耳机'), (3, '家居', '阅读台灯');
INSERT INTO channels (channel_id, name, region) VALUES
    (1, '华东官方商城', '华东'), (2, '华东微信小程序', '华东'), (3, '华南官方商城', '华南');
INSERT INTO orders (order_id, order_date, product_id, channel_id, status) VALUES
    (1001, '2026-11-16', 1, 1, 'completed'),
    (1002, '2026-11-17', 1, 1, 'completed'),
    (1003, '2026-11-18', 2, 1, 'completed'),
    (1004, '2026-11-19', 2, 2, 'completed'),
    (1005, '2026-11-20', 3, 2, 'completed'),
    (1006, '2026-11-21', 3, 3, 'completed'),
    (1007, '2026-11-22', 1, 3, 'completed'),
    (1008, '2026-11-22', 1, 1, 'cancelled'),
    (1009, '2026-11-23', 2, 1, 'completed'),
    (1010, '2026-11-15', 2, 1, 'completed'),
    (1011, '2026-11-22', 2, 1, 'pending'),
    (1012, '2026-11-22', 3, 1, 'completed');
INSERT INTO payments (order_id, amount, status) VALUES
    (1001, 300, 'paid'), (1002, 200, 'paid'), (1003, 800, 'paid'),
    (1004, 700, 'paid'), (1005, 120, 'paid'), (1006, 180, 'paid'),
    (1007, 160, 'paid'), (1009, 900, 'paid'), (1010, 500, 'paid'),
    (1011, 600, 'pending'), (1012, 130, 'paid');
INSERT INTO refunds (refund_id, order_id, amount, status) VALUES
    (1, 1001, 100, 'succeeded'), (2, 1003, 100, 'pending'),
    (3, 1004, 400, 'succeeded'), (4, 1004, 300, 'succeeded'),
    (5, 1006, 80, 'succeeded');
-- END SHARED FIXTURE

CREATE USER IF NOT EXISTS 'analytics_reader'@'%' IDENTIFIED BY 'analytics_demo';
GRANT SELECT ON ecommerce_analytics.products TO 'analytics_reader'@'%';
GRANT SELECT ON ecommerce_analytics.channels TO 'analytics_reader'@'%';
GRANT SELECT ON ecommerce_analytics.orders TO 'analytics_reader'@'%';
GRANT SELECT ON ecommerce_analytics.payments TO 'analytics_reader'@'%';
GRANT SELECT ON ecommerce_analytics.refunds TO 'analytics_reader'@'%';
