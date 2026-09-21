-- 0001_business_schema.sql
--
-- L-Mart business domain: the facts the AI employee investigates.
--
-- WHY A DEDICATED SCHEMA: Supabase automatically exposes the `public` schema
-- through PostgREST. Business data must be reachable only through our own tool
-- layer, where validation and authorization live -- not through an auto-generated
-- REST API. Putting it in `lmart` keeps it off that surface by default.
--
-- WHY EXPLICIT BIGINT PRIMARY KEYS (not identity/uuid): the seed generator
-- assigns ids deterministically so that regenerating the dataset produces
-- byte-identical output. Stable ids mean eval results are comparable across runs.
--
-- WHY MONEY IS BIGINT MINOR UNITS: never floats for money. All amounts are in
-- GBP pence. L-Mart trades in several countries, but we normalise to one
-- reporting currency at write time: FX conversion is a data-pipeline concern and
-- carrying it into every metric would add arithmetic noise without teaching
-- anything about agent design.
--
-- WHY timestamptz EVERYWHERE: every question in this system is "compared to
-- which period?". Ambiguous local times would silently corrupt every metric.

create schema if not exists lmart;


-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------

-- Physical stores. Justification: L-Mart is omnichannel, and "did engagement
-- drop because a store closed?" is an obvious hypothesis an investigator must
-- be able to check and rule out. A bare channel enum could not answer it.
create table lmart.stores (
    store_id      bigint primary key,
    code          text        not null unique,
    name          text        not null,
    country_code  char(2)     not null,
    city          text        not null,
    opened_on     date        not null,
    closed_on     date            null
);
comment on table lmart.stores is
    'Physical L-Mart stores. closed_on is null for trading stores.';


-- Product hierarchy is deliberately flat (no parent_category_id). Sub-category
-- analysis is not required by any question we intend to answer, and a
-- self-referencing hierarchy would force recursive CTEs into every metric.
create table lmart.categories (
    category_id   bigint primary key,
    code          text        not null unique,
    name          text        not null
);
comment on table lmart.categories is
    'Flat product categories. Category is the finest grain any metric needs.';


create table lmart.products (
    product_id    bigint primary key,
    sku           text        not null unique,
    name          text        not null,
    category_id   bigint      not null references lmart.categories,
    -- List price. The price actually charged is stored per order line, because
    -- list prices change over time and historical baskets must stay reconstructable.
    list_price_minor bigint   not null check (list_price_minor > 0)
);
create index products_category_idx on lmart.products (category_id);
comment on table lmart.products is
    'Sellable products. list_price_minor is current list price in GBP pence.';


-- ---------------------------------------------------------------------------
-- Customers and loyalty
-- ---------------------------------------------------------------------------

create table lmart.customers (
    customer_id   bigint primary key,
    external_ref  text        not null unique,
    full_name     text        not null,
    email         text        not null unique,
    country_code  char(2)     not null,
    city          text        not null,
    signed_up_on  date        not null,
    -- Consent is a hard gate on any future campaign action. It lives on the
    -- customer, not on the campaign, because it is a property of the person.
    marketing_opt_in boolean  not null default true
);
create index customers_country_idx on lmart.customers (country_code);
comment on table lmart.customers is
    'L-Mart shoppers. Not every customer is a loyalty member.';


-- Tier definitions as data, not as an enum. The agent can legitimately be asked
-- "what qualifies someone as Gold?" and that answer should come from the
-- database, not from a constant compiled into application code.
create table lmart.loyalty_tiers (
    tier_code         text primary key,
    name              text    not null,
    rank              int     not null unique,
    annual_spend_threshold_minor bigint not null,
    points_earn_multiplier numeric(4,2) not null
);
comment on table lmart.loyalty_tiers is
    'Loyalty tier definitions, ordered by rank (1 = lowest).';


-- Separated from customers because loyalty membership is opt-in and has its own
-- lifecycle. Keeping non-members in the dataset is what makes "engagement"
-- a meaningful segmentation rather than a property everyone has.
create table lmart.loyalty_accounts (
    account_id    bigint primary key,
    customer_id   bigint      not null unique references lmart.customers,
    enrolled_on   date        not null,
    current_tier  text        not null references lmart.loyalty_tiers,
    status        text        not null check (status in ('active','dormant','closed'))
);
create index loyalty_accounts_tier_idx on lmart.loyalty_accounts (current_tier);
comment on table lmart.loyalty_accounts is
    'One loyalty account per enrolled customer. current_tier is a cached '
    'convenience column; tier_history is the source of truth over time.';


-- CRITICAL TABLE. "Gold customers" is ambiguous: Gold *now*, or Gold *then*?
-- Without tier history, cohort analysis is impossible and composition effects
-- (high spenders promoted out of a tier, dragging its averages down) are
-- undetectable. This table is what makes that class of finding reachable.
create table lmart.tier_history (
    tier_history_id bigint primary key,
    account_id    bigint      not null references lmart.loyalty_accounts,
    tier_code     text        not null references lmart.loyalty_tiers,
    effective_from date       not null,
    effective_to  date            null,   -- null = currently in effect
    reason        text        not null check (reason in ('enrolment','upgrade','downgrade'))
);
create index tier_history_account_idx on lmart.tier_history (account_id, effective_from);
create index tier_history_window_idx  on lmart.tier_history (effective_from, effective_to);
comment on table lmart.tier_history is
    'Append-only record of which tier an account held over which date range. '
    'Use this, not loyalty_accounts.current_tier, for any historical question.';


-- ---------------------------------------------------------------------------
-- Transactions
-- ---------------------------------------------------------------------------

-- NOT DENORMALISED: country is deliberately absent here and must be joined from
-- customers. Copying it would invite drift between two sources of truth for the
-- single most-filtered dimension in the system. The metrics layer pays the join
-- cost once, in one place.
create table lmart.orders (
    order_id      bigint primary key,
    customer_id   bigint      not null references lmart.customers,
    store_id      bigint          null references lmart.stores,  -- null unless channel='store'
    channel       text        not null check (channel in ('store','web','app')),
    ordered_at    timestamptz not null,
    gross_amount_minor  bigint not null check (gross_amount_minor >= 0),
    discount_minor      bigint not null default 0 check (discount_minor >= 0),
    net_amount_minor    bigint not null check (net_amount_minor >= 0),
    constraint orders_store_matches_channel
        check ((channel = 'store') = (store_id is not null))
);
-- The single most important index in the schema: almost every metric is
-- "this customer / these customers, over this time window".
create index orders_customer_time_idx on lmart.orders (customer_id, ordered_at);
create index orders_time_idx          on lmart.orders (ordered_at);
create index orders_channel_time_idx  on lmart.orders (channel, ordered_at);
comment on table lmart.orders is
    'Order headers across all channels. net = gross - discount, in GBP pence.';


create table lmart.order_items (
    order_item_id bigint primary key,
    order_id      bigint      not null references lmart.orders,
    product_id    bigint      not null references lmart.products,
    quantity      int         not null check (quantity > 0),
    -- Price as charged, captured at sale time. Joining to products.list_price
    -- for historical revenue would silently restate the past when prices change.
    unit_price_minor bigint   not null check (unit_price_minor > 0),
    line_amount_minor bigint  not null check (line_amount_minor > 0)
);
create index order_items_order_idx   on lmart.order_items (order_id);
create index order_items_product_idx on lmart.order_items (product_id);
comment on table lmart.order_items is
    'Order lines. unit_price_minor is the price charged at the time of sale.';


-- ---------------------------------------------------------------------------
-- Points
-- ---------------------------------------------------------------------------

-- APPEND-ONLY LEDGER, not a balance column. Balance is sum(points). This is how
-- real loyalty systems work, and it is the only design that can answer "why did
-- this customer's balance change?" -- which is exactly the kind of question the
-- AI employee exists to answer. A mutable balance column destroys that history.
create table lmart.points_ledger (
    entry_id      bigint primary key,
    account_id    bigint      not null references lmart.loyalty_accounts,
    occurred_at   timestamptz not null,
    entry_type    text        not null check (entry_type in ('earn','redeem','expire','adjust')),
    -- Signed delta: positive for earn, negative for redeem/expire.
    points        int         not null,
    order_id      bigint          null references lmart.orders,
    reason        text            null,
    constraint points_sign_matches_type check (
        (entry_type = 'earn'   and points > 0) or
        (entry_type = 'redeem' and points < 0) or
        (entry_type = 'expire' and points < 0) or
        (entry_type = 'adjust')
    )
);
create index points_ledger_account_time_idx on lmart.points_ledger (account_id, occurred_at);
create index points_ledger_type_time_idx    on lmart.points_ledger (entry_type, occurred_at);
comment on table lmart.points_ledger is
    'Append-only points movements. Balance = sum(points) for an account. '
    'Never update or delete rows here.';


-- ---------------------------------------------------------------------------
-- Campaigns
-- ---------------------------------------------------------------------------

-- Modelled as one row per SEND WAVE, not one row per recurring programme.
-- Reason: CRM performance is measured per wave, and modelling it this way makes
-- a programme that stopped running structurally visible -- there are simply no
-- rows after the last wave. A single row with an end date would hide the
-- cadence, which is the thing that actually changed.
create table lmart.campaigns (
    campaign_id   bigint primary key,
    code          text        not null unique,
    name          text        not null,
    programme     text        not null,   -- groups recurring waves together
    objective     text        not null,
    channel       text        not null check (channel in ('email','push','sms')),
    target_description text   not null,   -- human-readable audience rule
    status        text        not null check (status in ('completed','active','cancelled')),
    sent_on       date        not null
);
create index campaigns_programme_idx on lmart.campaigns (programme, sent_on);
comment on table lmart.campaigns is
    'One row per campaign send wave. Recurring programmes share a programme name.';


create table lmart.campaign_events (
    event_id      bigint primary key,
    campaign_id   bigint      not null references lmart.campaigns,
    customer_id   bigint      not null references lmart.customers,
    event_type    text        not null check (event_type in
                      ('sent','delivered','opened','clicked','converted','unsubscribed')),
    occurred_at   timestamptz not null,
    -- Set only on 'converted', linking the outcome to the actual order. This is
    -- what keeps campaign performance consistent with transaction data instead
    -- of being an independently invented number.
    order_id      bigint          null references lmart.orders
);
create index campaign_events_campaign_type_idx on lmart.campaign_events (campaign_id, event_type);
create index campaign_events_customer_idx      on lmart.campaign_events (customer_id, occurred_at);
comment on table lmart.campaign_events is
    'Per-recipient campaign funnel events. Engagement rates are derived from '
    'these, never stored as aggregates.';
