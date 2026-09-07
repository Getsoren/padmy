# Padmy

[![Python versions](https://img.shields.io/pypi/pyversions/padmy)](https://pypi.python.org/pypi/padmy)
[![Latest PyPI version](https://img.shields.io/pypi/v/padmy?logo=pypi)](https://pypi.python.org/pypi/padmy)
[![CI](https://github.com/Getsoren/padmy/actions/workflows/ci.yml/badge.svg)](https://github.com/Getsoren/padmy/actions)

CLI utility functions for PostgreSQL such as **sampling** and **anonymization**.

## Installation

Just run

```bash
uv add padmy
```

## 1. Database Exploration

You can get information about a database by running

```bash
uvx padmy analyze --db test --schemas test
```

or using the docker image

```bash
 docker run -it \
   --network host \
   ghcr.io/soren/padmy:latest analyze --db test --schemas test
```

For instance, the following table definition will output:

```sql
CREATE TABLE table1
(
    id SERIAL PRIMARY KEY
);

CREATE TABLE table2
(
    id        SERIAL PRIMARY KEY,
    table1_id INT REFERENCES table1
);

CREATE TABLE table3
(
    id        SERIAL PRIMARY KEY,
    table1_id INT REFERENCES table1,
    table2_id INT REFERENCES table2
);

CREATE TABLE table4
(
    id        SERIAL PRIMARY KEY,
    table1_id INT REFERENCES table1
);
INSERT INTO table1(id)
SELECT generate_series(0, 10);
```

**Default**

![Network schema](./docs/explore-default.png)

**Network Schema** (if `--show-graphs` is specified)

![Network schema](./docs/explore-schema.png)

## 2. Sampling

You can quickly sample (ie: take a subset) of a database by running

```bash
uvx padmy sample \
  --db test --to-db test-sampled \
  --sample 20 \
  --schemas public
```

This will sample the `test` database into a new `test-sampled` database, copy of the
original one, keeping if possible (see: [Annexe](#Known-limitations)) **20%** of the original database.

You can choose how to sample with more granularity by passing a configuration file.
Here is an example:

```yaml
# We want a default sampling size of 20% of each table count
sample: 20
# We want to sample `schema_1` and `schema_2`
schemas:
  - schema_1
  # We want a default size of 30% for the tables of this schema
  - name: schema_2
    sample: 30

tables:
  # We want a sample size of 10% for this table
  - schema: public
    table: table_3
    sample: 10
```

## 3. Anonymization

You can scrub PII from selected columns with `padmy anonymize`. Field types map
to Faker generators (or `NULL` to blank the column). Available types:

| Type | Behavior |
| --- | --- |
| `EMAIL` | `faker.email()` — supports `domain:` extra arg |
| `NULL` | Sets the column to `NULL` (useful for hashes, tokens, anything you don't want to fake) |
| `FIRST_NAME` | `faker.first_name()` |
| `LAST_NAME` | `faker.last_name()` |
| `NAME` | `faker.name()` (full name) |
| `PHONE_NUMBER` | `faker.phone_number()` |
| `DATE_OF_BIRTH` | `faker.date_of_birth()` — supports `minimum_age:` / `maximum_age:` |
| `TEXT` | `faker.text()` — supports `max_nb_chars:` |
| `WORD` | `faker.word()` |

Example config:

```yaml
tables:
  - schema: public
    table: users
    fields:
      - column: email
        type: EMAIL
        domain: example.com         # extra arg forwarded to faker.email
      - column: password_hash
        type: "NULL"                # quoted: YAML's bare NULL parses as null
      - column: first_name
        type: FIRST_NAME
      - column: birthdate
        type: DATE_OF_BIRTH
        minimum_age: 18
        maximum_age: 80
```

Run with:

```bash
uvx padmy anonymize --db test -f config.yml
```

## 4. Migration utils

**Setting up**

This library includes a migration utility to help you evolve your data model.
In order to use it, start by setting up the migration table:

```bash
uvx padmy -vv migrate setup --db postgres
```

This will create the `public.migration` table that stores all the migration / rollback that
will be applied.

**Setting up the Schemas**

Now that we are all setup, let's create our first sql file that will create the schema:

```bash
rm -rf /tmp/sql
mkdir /tmp/sql
uvx padmy -v migrate new-sql 1 --sql-dir /tmp/sql
tree /tmp/sql
```  

Add `CREATE SCHEMA general;` to the file.

```bash
echo "CREATE SCHEMA general;" >> /tmp/sql/0001_new_file.sql
cat /tmp/sql/0001_new_file.sql
```

Then apply the modifications to the database:

```bash
uvx padmy -v migrate apply-sql --sql-dir /tmp/sql --db postgres 
```

> [!WARNING]
> This will run through all the files in the `/tmp/sql` folder (in order) run them.
> Sql files here **need to be IDEMPOTENT**

**Creating a first migration**

Now, lets create our first migration:

```bash
migration_dir="/tmp/migrations"
rm -rf "$migration_dir"
mkdir "$migration_dir" # You can choose a different folder to store your migrations
uvx padmy -v migrate new --sql-dir "$migration_dir" --author padmy
tree "$migration_dir"
```

This will create 2 new files:

- **up**: `{timestamp}-{migration_id}-up.sql` that contains your
  migration to apply to the database.
- **down**: `{timestamp}-{migration_id}-down.sql` that contains the code to revert your changes.

> [!NOTE]  
> A migration contains:
> - a reference to the previous file (if any): `-- Prev-file: {timestamp}-{migration_id}`
> - the name of the author who created the migration: `-- Author: {author}`
> - (optionally) should the down migration be ignored: `-- Skip-verify: {reason}`


Let's now modify the `up.sql` file with:

```sql
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
```

```bash
file="$migration_dir/$(ls "$migration_dir" | grep up.sql)"
printf "\n" >> "$file"
cat <<'SQL' >> "$file"
CREATE TABLE IF NOT EXISTS general.test (
    id  INT PRIMARY KEY,
    foo INT
);

CREATE TABLE IF NOT EXISTS general.test2 (
    id  SERIAL PRIMARY KEY,
    foo TEXT
);
SQL
```

and check that the migration is valid:

```bash
uvx padmy -v migrate verify --sql-dir /tmp/migrations --schemas general
``` 

Because we did not add anything to the `down.sql` file, the command returns an error.
Let's modify it to make the command pass:

```sql
DROP table general.test;
DROP table general.test2;
``` 

```bash
uvx padmy -vv migrate verify --sql-dir /tmp/migrations
``` 

We are all good !

**Optional**: You can also verify that the order of the migration is correct by running:

```bash
uvx padmy -vv migrate verify-files --sql-dir /tmp/migrations --no-raise
```

## 5. Comparing databases schemas

You can compare two databases by running:

```bash
uvx padmy -vv schema-diff --db soren --schemas schema_1,schema_2
```

If differences are found, the command will output the differences between the two databases.

### Known limitations

**Exact sample size**

Sometimes, we cannot guaranty that the sampled table will have the exact
expected size.

For instance let's say we want **10%** of *table1* and **10%** of *table2*, given the following
table definitions:

```sql
CREATE TABLE table1
(
    id SERIAL PRIMARY KEY
);

CREATE TABLE table2
(
    id        SERIAL PRIMARY KEY,
    table1_id INT NOT NULL REFERENCES table1
);

INSERT INTO table1(id)
VALUES (1);

INSERT INTO table2(table1_id)
SELECT 1
FROM generate_series(1, 10);
```

In this case, it's not possible to have less that **100%** of table 1 since it has only 1 key on
which depend all the `table1_id` rows of *table2*.

**Cyclic foreign keys**

Cyclic foreign keys (table with a FK on another table that reference the previous one) are not supported.
Here is an example.

```sql
CREATE TABLE table1
(
    id        SERIAL PRIMARY KEY,
    table2_id INT NOT NULL
);

CREATE TABLE table2
(
    id        SERIAL PRIMARY KEY,
    table1_id INT NOT NULL
);

ALTER TABLE table1
    ADD CONSTRAINT table1_table2_id_fk
        FOREIGN KEY (table2_id) REFERENCES table2;

ALTER TABLE table2
    ADD CONSTRAINT table2_table1_id_fk
        FOREIGN KEY (table1_id) REFERENCES table1;
```

![Cyclic dependencies](./docs/cyclic-deps.png)

You can display cycling dependencies in a database by running:

```bash
uvx padmy -vv analyze --db test --schemas test --show-graph
```

(**Note::** you'll need to have installed the `network` extra )

**Self referencing foreign keys**

Foreign keys referencing another column in the same table are ignored.

```sql
CREATE TABLE table1
(
    id        SERIAL PRIMARY KEY,
    parent_id INT REFERENCES table1
);
```

# Annexes

## Showing Network in Jupyter

You can display the network visualization in Jupyter.

Start by launching a JupyterLab session with:

```bash
uv run --group notebook jupyter lab
```

Then create a new notebook and run the following code:

```python
from dash import Dash
from padmy.sampling import network, viz, sampling
from padmy.utils import init_connection
import asyncpg

PG_URL = 'postgresql://postgres:postgres@localhost:5432/test'

app = Dash(__name__)

db = sampling.Database(name='test')

async with asyncpg.create_pool(PG_URL, init=init_connection) as pool:
    await db.explore(pool, ['public'])

g = network.convert_db(db)

app.layout = viz.get_layout(g,
                            style={'width': '100%', 'height': '800px'},
                            layout='klay')

app.run(jupyter_mode='jupyterlab')  # or jupyter_mode='inline'
```
