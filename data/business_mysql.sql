SET NAMES utf8mb4;
CREATE DATABASE IF NOT EXISTS ecommerce_support CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE ecommerce_support;

CREATE TABLE IF NOT EXISTS orders (
    id VARCHAR(40) PRIMARY KEY,
    user_id VARCHAR(80) NOT NULL,
    product VARCHAR(255) NOT NULL,
    status VARCHAR(80) NOT NULL,
    logistics_status VARCHAR(80) NOT NULL,
    tracking VARCHAR(80) NOT NULL,
    logistics_detail VARCHAR(255) NOT NULL,
    refund_id VARCHAR(80) NOT NULL,
    refund_status VARCHAR(80) NOT NULL,
    amount_cents INT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id VARCHAR(40) PRIMARY KEY,
    user_id VARCHAR(80) NOT NULL,
    run_id VARCHAR(40) NOT NULL UNIQUE,
    reason VARCHAR(255) NOT NULL,
    order_id VARCHAR(40),
    status VARCHAR(30) NOT NULL,
    owner VARCHAR(80),
    created_at VARCHAR(40) NOT NULL,
    INDEX tickets_by_customer (user_id, created_at)
);

INSERT IGNORE INTO orders VALUES
('ORD-1001','alice','云感运动鞋','已支付','运输中','YT731001','已到达杭州转运中心','','',32900),
('ORD-1002','alice','恒温随行杯','已完成','已签收','SF731002','已由本人签收','REF-1002','退款处理中',15900),
('ORD-2001','bob','降噪耳机','已完成','已签收','JD732001','已送达指定地址','','',49900);

CREATE USER IF NOT EXISTS 'support_demo'@'%' IDENTIFIED BY 'support_demo_local';
GRANT SELECT ON ecommerce_support.orders TO 'support_demo'@'%';
GRANT SELECT, INSERT, UPDATE ON ecommerce_support.tickets TO 'support_demo'@'%';
