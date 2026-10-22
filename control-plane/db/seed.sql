-- Dữ liệu minh họa cho đủ 9 bảng trong app/models.py.
-- Tạo bảng trước bằng python -m app.init_db, sau đó chạy:
--   psql "<database url>" -v ON_ERROR_STOP=1 -f db/seed.sql
-- Chỉ nạp một lần; chạy lại sẽ lỗi trùng dữ liệu và rollback transaction.
-- Không gán ID thủ công để giữ đúng sequence tự tăng của PostgreSQL.
-- User demo bị vô hiệu hóa, không dùng để đăng nhập. Khóa SSH/API chỉ để
-- minh họa; API key đã bị thu hồi. Giá và chi phí không phải báo giá thật.

BEGIN;

INSERT INTO users (name, email, username, password_hash, is_active) VALUES
    ('Spulti Demo', 'demo@example.com', 'spulti_demo', '!seed-account-disabled', false);

INSERT INTO ssh_keys (user_id, name, public_key, fingerprint)
SELECT id, 'demo-laptop',
       'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f demo@example.com',
       'SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA'
FROM users WHERE username = 'spulti_demo';

INSERT INTO api_keys (user_id, name, key_prefix, key_hash, last_used_at,
                      expires_at, revoked_at)
SELECT id, 'demo-cli-revoked', 'spulti_seed',
       'bdab6b6d545d7de49c757ca27068ad3db9df833eab7455521dcc0d159cb31061',
       '2026-10-20 08:00:00+00', '2026-10-21 00:00:00+00',
       '2026-10-20 09:00:00+00'
FROM users WHERE username = 'spulti_demo';

INSERT INTO machine_types (provider, name, vcpu, ram_gib, gpu_count) VALUES
    ('AWS', 't3.medium', 2, 4.00, 0),
    ('GCP', 'e2-medium', 2, 4.00, 0);

INSERT INTO machines (user_id, name, requested_vcpu, requested_ram_gib,
                      requested_gpu_count, requested_gpu_type,
                      optimization_mode, status, created_at)
SELECT id, 'demo-training-aws', 2, 4.00, 0, NULL, 'SAVING', 'RUNNING',
       '2026-10-20 07:55:00+00'
FROM users WHERE username = 'spulti_demo';

INSERT INTO machines (user_id, name, requested_vcpu, requested_ram_gib,
                      requested_gpu_count, requested_gpu_type,
                      optimization_mode, status, created_at)
SELECT id, 'demo-api-gcp', 2, 4.00, 0, NULL, 'STABLE', 'RUNNING',
       '2026-10-20 07:55:00+00'
FROM users WHERE username = 'spulti_demo';

-- Instance AWS đầu tiên bị thu hồi lúc 09:00 UTC.
INSERT INTO instances (machine_id, machine_type_id, provider_instance_id,
                       region, zone, started_at, ended_at)
SELECT m.id, mt.id, 'i-seed-aws-old', 'ap-southeast-1', 'ap-southeast-1a',
       '2026-10-20 08:00:00+00', '2026-10-20 09:00:00+00'
FROM machines m
JOIN users u ON u.id = m.user_id
CROSS JOIN machine_types mt
WHERE u.username = 'spulti_demo' AND m.name = 'demo-training-aws'
  AND mt.provider = 'AWS' AND mt.name = 't3.medium';

-- Instance thay thế vẫn thuộc cùng máy logic demo-training-aws.
INSERT INTO instances (machine_id, machine_type_id, provider_instance_id,
                       region, zone, started_at)
SELECT m.id, mt.id, 'i-seed-aws-new', 'ap-southeast-1', 'ap-southeast-1b',
       '2026-10-20 09:05:00+00'
FROM machines m
JOIN users u ON u.id = m.user_id
CROSS JOIN machine_types mt
WHERE u.username = 'spulti_demo' AND m.name = 'demo-training-aws'
  AND mt.provider = 'AWS' AND mt.name = 't3.medium';

INSERT INTO instances (machine_id, machine_type_id, provider_instance_id,
                       region, zone, started_at)
SELECT m.id, mt.id, 'seed-gcp-api-01', 'asia-southeast1', 'asia-southeast1-a',
       '2026-10-20 08:00:00+00'
FROM machines m
JOIN users u ON u.id = m.user_id
CROSS JOIN machine_types mt
WHERE u.username = 'spulti_demo' AND m.name = 'demo-api-gcp'
  AND mt.provider = 'GCP' AND mt.name = 'e2-medium';

INSERT INTO spot_prices (machine_type_id, region, zone, price_type,
                         price_per_hour, recorded_at)
SELECT id, 'ap-southeast-1', 'ap-southeast-1a', 'SPOT', 0.015000,
       '2026-10-20 08:00:00+00'
FROM machine_types WHERE provider = 'AWS' AND name = 't3.medium';

INSERT INTO spot_prices (machine_type_id, region, zone, price_type,
                         price_per_hour, recorded_at)
SELECT id, 'ap-southeast-1', 'ap-southeast-1b', 'SPOT', 0.016000,
       '2026-10-20 09:00:00+00'
FROM machine_types WHERE provider = 'AWS' AND name = 't3.medium';

INSERT INTO spot_prices (machine_type_id, region, zone, price_type,
                         price_per_hour, recorded_at)
SELECT id, 'ap-southeast-1', NULL, 'ON_DEMAND', 0.050000,
       '2026-10-20 08:00:00+00'
FROM machine_types WHERE provider = 'AWS' AND name = 't3.medium';

INSERT INTO spot_prices (machine_type_id, region, zone, price_type,
                         price_per_hour, recorded_at)
SELECT id, 'asia-southeast1', 'asia-southeast1-a', 'SPOT', 0.012000,
       '2026-10-20 08:00:00+00'
FROM machine_types WHERE provider = 'GCP' AND name = 'e2-medium';

INSERT INTO spot_prices (machine_type_id, region, zone, price_type,
                         price_per_hour, recorded_at)
SELECT id, 'asia-southeast1', NULL, 'ON_DEMAND', 0.040000,
       '2026-10-20 08:00:00+00'
FROM machine_types WHERE provider = 'GCP' AND name = 'e2-medium';

INSERT INTO events (machine_id, instance_id, event_type, details, created_at)
SELECT i.machine_id, i.id, 'RECLAIM',
       '{"message":"Demo spot instance reclaimed","replacement_instance_id":"i-seed-aws-new"}',
       '2026-10-20 09:00:00+00'
FROM instances i
JOIN machine_types mt ON mt.id = i.machine_type_id
WHERE mt.provider = 'AWS' AND mt.name = 't3.medium'
  AND i.provider_instance_id = 'i-seed-aws-old';

-- Chi phí minh họa tính đến 10:05 UTC: AWS cũ 1 giờ, AWS mới 1 giờ,
-- GCP 2 giờ 5 phút chạy ON_DEMAND (máy chọn chế độ STABLE).
INSERT INTO cost_records (instance_id, actual_cost, on_demand_cost, calculated_at)
SELECT i.id, 0.015000, 0.050000, '2026-10-20 10:05:00+00'
FROM instances i
JOIN machine_types mt ON mt.id = i.machine_type_id
WHERE mt.provider = 'AWS' AND mt.name = 't3.medium'
  AND i.provider_instance_id = 'i-seed-aws-old';

INSERT INTO cost_records (instance_id, actual_cost, on_demand_cost, calculated_at)
SELECT i.id, 0.016000, 0.050000, '2026-10-20 10:05:00+00'
FROM instances i
JOIN machine_types mt ON mt.id = i.machine_type_id
WHERE mt.provider = 'AWS' AND mt.name = 't3.medium'
  AND i.provider_instance_id = 'i-seed-aws-new';

INSERT INTO cost_records (instance_id, actual_cost, on_demand_cost, calculated_at)
SELECT i.id, 0.083333, 0.083333, '2026-10-20 10:05:00+00'
FROM instances i
JOIN machine_types mt ON mt.id = i.machine_type_id
WHERE mt.provider = 'GCP' AND mt.name = 'e2-medium'
  AND i.provider_instance_id = 'seed-gcp-api-01';

COMMIT;
