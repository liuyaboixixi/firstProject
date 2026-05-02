-- Minimal MySQL schema for javaProject SQL review testing
-- Source references:
-- - javaProject/src/main/resources/application.properties
-- - javaProject/src/main/resources/generatorConfig.xml
-- - javaProject/src/main/resources/mapper/*.xml

CREATE DATABASE IF NOT EXISTS `yuyin`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_general_ci;

USE `yuyin`;

DROP TABLE IF EXISTS `notification`;
DROP TABLE IF EXISTS `comment`;
DROP TABLE IF EXISTS `question`;
DROP TABLE IF EXISTS `user`;

CREATE TABLE `user` (
  `id` BIGINT NOT NULL,
  `account_id` VARCHAR(100) DEFAULT NULL,
  `name` VARCHAR(100) DEFAULT NULL,
  `token` VARCHAR(255) DEFAULT NULL,
  `gmt_create` BIGINT DEFAULT NULL,
  `gmt_modified` BIGINT DEFAULT NULL,
  `password` VARCHAR(255) DEFAULT NULL,
  `avatarurl` VARCHAR(512) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_account_id` (`account_id`),
  KEY `idx_user_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `question` (
  `id` BIGINT NOT NULL,
  `title` VARCHAR(255) DEFAULT NULL,
  `gmt_create` BIGINT DEFAULT NULL,
  `gmt_modified` BIGINT DEFAULT NULL,
  `creator` BIGINT DEFAULT NULL,
  `comment_count` INT DEFAULT 0,
  `view_count` INT DEFAULT 0,
  `like_count` INT DEFAULT 0,
  `tag` VARCHAR(255) DEFAULT NULL,
  `description` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_question_creator` (`creator`),
  KEY `idx_question_gmt_create` (`gmt_create`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `comment` (
  `id` BIGINT NOT NULL,
  `parent_id` BIGINT DEFAULT NULL,
  `type` INT DEFAULT NULL,
  `commentator` BIGINT DEFAULT NULL,
  `gmt_create` BIGINT DEFAULT NULL,
  `gmt_modified` BIGINT DEFAULT NULL,
  `like_count` INT DEFAULT 0,
  `content` VARCHAR(1000) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_comment_parent_id` (`parent_id`),
  KEY `idx_comment_commentator` (`commentator`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `notification` (
  `id` BIGINT NOT NULL,
  `notifier` BIGINT DEFAULT NULL,
  `receiver` BIGINT DEFAULT NULL,
  `outerid` BIGINT DEFAULT NULL,
  `type` INT DEFAULT NULL,
  `gmt_create` BIGINT DEFAULT NULL,
  `status` INT DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_notification_receiver_status` (`receiver`, `status`),
  KEY `idx_notification_notifier` (`notifier`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `user` (`id`, `account_id`, `name`, `token`, `gmt_create`, `gmt_modified`, `password`, `avatarurl`) VALUES
(1, 'u1001', 'alice', 'token-alice', 1714500000, 1714500000, 'pass-alice', 'https://img.local/alice.png'),
(2, 'u1002', 'bob',   'token-bob',   1714500100, 1714500100, 'pass-bob',   'https://img.local/bob.png'),
(3, 'u1003', 'carol', 'token-carol', 1714500200, 1714500200, 'pass-carol', 'https://img.local/carol.png');

INSERT INTO `question` (`id`, `title`, `gmt_create`, `gmt_modified`, `creator`, `comment_count`, `view_count`, `like_count`, `tag`, `description`) VALUES
(101, 'How to use MyBatis',       1714501000, 1714501000, 1, 2, 120, 5, 'java,mybatis,spring', 'question description 101'),
(102, 'GitLab SQL Review Design', 1714501100, 1714501100, 1, 1,  80, 3, 'gitlab,sql,review',   'question description 102'),
(103, 'Regex search on title',    1714501200, 1714501200, 2, 0,  60, 2, 'mysql,regexp',        'question description 103'),
(104, 'Dynamic SQL paging',       1714501300, 1714501300, 2, 3, 150, 9, 'mybatis,paging',      'question description 104'),
(105, 'Community project setup',  1714501400, 1714501400, 3, 1,  33, 1, 'setup,community',     'question description 105');

INSERT INTO `comment` (`id`, `parent_id`, `type`, `commentator`, `gmt_create`, `gmt_modified`, `like_count`, `content`) VALUES
(1001, 101, 1, 2, 1714502000, 1714502000, 0, 'first comment for question 101'),
(1002, 101, 1, 3, 1714502100, 1714502100, 1, 'second comment for question 101'),
(1003, 104, 1, 1, 1714502200, 1714502200, 0, 'comment for question 104');

INSERT INTO `notification` (`id`, `notifier`, `receiver`, `outerid`, `type`, `gmt_create`, `status`) VALUES
(2001, 2, 1, 101, 1, 1714503000, 0),
(2002, 3, 1, 104, 1, 1714503100, 0),
(2003, 1, 2, 1001, 2, 1714503200, 1);

-- Notes:
-- 1. This script intentionally does not create an index on question.title or question.tag.
--    That lets the SQL review detect likely index risks for regexp/title/tag queries.
-- 2. If you want to verify "index exists and is used" logic later, you can add:
--      CREATE INDEX idx_question_title ON question(title);
--      CREATE INDEX idx_question_tag ON question(tag);
