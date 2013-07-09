CREATE TABLE `prosodyarchive_checkpoint` (
  `host` text NOT NULL,
  `user` text NOT NULL,
  `with` text NOT NULL,
  `last_id` integer DEFAULT NULL,
  PRIMARY KEY (`host`(20),`user`(20),`with`(30))
);
