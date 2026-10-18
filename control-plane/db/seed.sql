--
-- PostgreSQL database dump
--

\restrict 3gqPQu13PiintxFUk6m3rrjZwmnIy6jxPeU7vRQGuLmvbZdmCYOcOtcz2YMHeCm

-- Dumped from database version 18.6
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Data for Name: machine_types; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.machine_types (id, provider, name, vcpu, ram_gib, gpu_count) VALUES (1, 'AWS', 't3.medium', 2, 4.00, 0);
INSERT INTO public.machine_types (id, provider, name, vcpu, ram_gib, gpu_count) VALUES (2, 'GCP', 'e2-medium', 2, 4.00, 0);


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.users (id, name, email, username, password_hash, created_at) VALUES (3, 'spulter', 'demo@spulti.com', 'spulrealer', 'fake_hash_123', '2026-10-18 17:00:10.456042+07');


--
-- Data for Name: machines; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.machines (id, user_id, name, requested_vcpu, requested_ram_gib, optimization_mode, status, created_at, requested_gpu_count, requested_gpu_type) VALUES (1, 3, 'training-01', 2, 4.00, 'BALANCED', 'RUNNING', '2026-10-18 17:01:18.423342+07', 0, NULL);


--
-- Data for Name: instances; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.instances (id, machine_id, machine_type_id, provider_instance_id, region, zone, started_at, ended_at) VALUES (1, 1, 1, 'i-demo-123', 'ap-southeast-1', 'ap-southeast-1a', '2026-10-18 17:01:39.971247+07', NULL);


--
-- Data for Name: cost_records; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.cost_records (id, instance_id, actual_cost, on_demand_cost, calculated_at) VALUES (1, 1, 0.015000, 0.050000, '2026-10-18 17:02:50.645317+07');


--
-- Data for Name: events; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.events (id, machine_id, instance_id, event_type, details, created_at) VALUES (2, 1, 1, 'RECLAIM', '{"message": "Instance RECLAIMING"}', '2026-10-18 17:02:41.595167+07');


--
-- Data for Name: spot_prices; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.spot_prices (id, machine_type_id, region, zone, price_type, price_per_hour, recorded_at) VALUES (1, 1, 'ap-southeast-1', 'ap-southeast-1a', 'SPOT', 0.015000, '2026-10-18 17:01:55.218616+07');


--
-- Name: cost_records_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.cost_records_id_seq', 1, true);


--
-- Name: events_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.events_id_seq', 2, true);


--
-- Name: instances_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.instances_id_seq', 1, true);


--
-- Name: machine_types_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.machine_types_id_seq', 3, true);


--
-- Name: machines_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.machines_id_seq', 3, true);


--
-- Name: spot_prices_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.spot_prices_id_seq', 1, true);


--
-- Name: users_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.users_id_seq', 4, true);


--
-- PostgreSQL database dump complete
--

\unrestrict 3gqPQu13PiintxFUk6m3rrjZwmnIy6jxPeU7vRQGuLmvbZdmCYOcOtcz2YMHeCm

