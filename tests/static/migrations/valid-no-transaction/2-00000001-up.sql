-- Prev-file: 1-00000000-up.sql
-- Author: foo@bar.baz
-- No-transaction: true

CREATE INDEX CONCURRENTLY test_foo_idx ON general.test (foo);
