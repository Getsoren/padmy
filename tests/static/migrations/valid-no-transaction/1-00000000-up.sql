-- Prev-file:
-- Author: foo@bar.baz

CREATE TABLE IF NOT EXISTS general.test
(
    id  int primary key,
    foo int
);

CREATE TABLE IF NOT EXISTS general.test2
(
    id  serial primary key,
    foo text
);
