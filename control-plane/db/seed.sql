-- Sample data for development. Run after the tables exist (python -m app.init_db):
--   psql "<database url>" -f db/seed.sql
-- IDs are not hard-coded; rows are looked up by name instead.

BEGIN;

INSERT INTO machine_types (provider, name, vcpu, ram_gib, gpu_count) VALUES
    ('AWS', 't3.medium', 2, 4.00, 0),
    ('GCP', 'e2-medium', 2, 4.00, 0);

INSERT INTO users (name, email, username, password_hash) VALUES
    ('spulter', 'demo@spulti.com', 'spulrealer', 'fake_hash_123');

INSERT INTO machines (user_id, name, requested_vcpu, requested_ram_gib,
                      optimization_mode, status)
SELECT id, 'training-01', 2, 4.00, 'BALANCED', 'RUNNING'
FROM users WHERE username = 'spulrealer';

INSERT INTO instances (machine_id, machine_type_id, provider_instance_id,
                       region, zone)
SELECT m.id, mt.id, 'i-demo-123', 'ap-southeast-1', 'ap-southeast-1a'
FROM machines m, machine_types mt
WHERE m.name = 'training-01' AND mt.provider = 'AWS' AND mt.name = 't3.medium';

INSERT INTO spot_prices (machine_type_id, region, zone, price_type,
                         price_per_hour, recorded_at)
SELECT id, 'ap-southeast-1', 'ap-southeast-1a', 'SPOT', 0.015, now()
FROM machine_types WHERE provider = 'AWS' AND name = 't3.medium';

INSERT INTO events (machine_id, instance_id, event_type, details)
SELECT i.machine_id, i.id, 'RECLAIM', '{"message": "Instance RECLAIMING"}'
FROM instances i WHERE i.provider_instance_id = 'i-demo-123';

INSERT INTO cost_records (instance_id, actual_cost, on_demand_cost)
SELECT id, 0.015, 0.050
FROM instances WHERE provider_instance_id = 'i-demo-123';

COMMIT;
