--
-- PostgreSQL database dump
--

\restrict Sz16f0HSK1Kf4AGnnO0mOuc9oAXCHVfFGes78afrayhggwhWoCOxfwRZbT5yLsO

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

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: cost_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cost_records (
    id integer NOT NULL,
    instance_id integer NOT NULL,
    actual_cost numeric(12,6) DEFAULT 0 NOT NULL,
    on_demand_cost numeric(12,6) DEFAULT 0 NOT NULL,
    calculated_at timestamp with time zone DEFAULT now()
);


--
-- Name: cost_records_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.cost_records_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: cost_records_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.cost_records_id_seq OWNED BY public.cost_records.id;


--
-- Name: events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.events (
    id integer NOT NULL,
    machine_id integer NOT NULL,
    instance_id integer,
    event_type character varying(50) NOT NULL,
    details jsonb,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT events_event_type_check CHECK (((event_type)::text = ANY ((ARRAY['RECLAIM'::character varying, 'INTERRUPT'::character varying])::text[])))
);


--
-- Name: events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.events_id_seq OWNED BY public.events.id;


--
-- Name: instances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instances (
    id integer NOT NULL,
    machine_id integer NOT NULL,
    machine_type_id integer NOT NULL,
    provider_instance_id character varying(255) NOT NULL,
    region character varying(50) NOT NULL,
    zone character varying(50) NOT NULL,
    started_at timestamp with time zone DEFAULT now(),
    ended_at timestamp with time zone
);


--
-- Name: instances_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.instances_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: instances_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.instances_id_seq OWNED BY public.instances.id;


--
-- Name: machine_types; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.machine_types (
    id integer NOT NULL,
    provider character varying(10) NOT NULL,
    name character varying(100) NOT NULL,
    vcpu integer NOT NULL,
    ram_gib numeric(10,2) NOT NULL,
    gpu_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT machine_types_provider_check CHECK (((provider)::text = ANY ((ARRAY['AWS'::character varying, 'GCP'::character varying])::text[]))),
    CONSTRAINT machine_types_ram_gib_check CHECK ((ram_gib > (0)::numeric)),
    CONSTRAINT machine_types_vcpu_check CHECK ((vcpu > 0))
);


--
-- Name: machine_types_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.machine_types_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: machine_types_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.machine_types_id_seq OWNED BY public.machine_types.id;


--
-- Name: machines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.machines (
    id integer NOT NULL,
    user_id integer NOT NULL,
    name character varying(100) NOT NULL,
    requested_vcpu integer NOT NULL,
    requested_ram_gib numeric(10,2) NOT NULL,
    optimization_mode character varying(20) NOT NULL,
    status character varying(20) DEFAULT 'PENDING'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT machines_optimization_mode_check CHECK (((optimization_mode)::text = ANY ((ARRAY['SAVING'::character varying, 'STABLE'::character varying, 'BALANCED'::character varying])::text[]))),
    CONSTRAINT machines_status_check CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'PROVISIONING'::character varying, 'RUNNING'::character varying])::text[])))
);


--
-- Name: machines_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.machines_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: machines_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.machines_id_seq OWNED BY public.machines.id;


--
-- Name: spot_prices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.spot_prices (
    id integer NOT NULL,
    machine_type_id integer NOT NULL,
    region character varying(50) NOT NULL,
    zone character varying(50),
    price_type character varying(20) NOT NULL,
    price_per_hour numeric(12,6) NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    CONSTRAINT spot_prices_price_per_hour_check CHECK ((price_per_hour >= (0)::numeric)),
    CONSTRAINT spot_prices_price_type_check CHECK (((price_type)::text = ANY ((ARRAY['SPOT'::character varying, 'ON_DEMAND'::character varying])::text[])))
);


--
-- Name: spot_prices_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.spot_prices_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: spot_prices_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.spot_prices_id_seq OWNED BY public.spot_prices.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id integer NOT NULL,
    name character varying(50) NOT NULL,
    email character varying(255) NOT NULL,
    username character varying(255) NOT NULL,
    password_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: cost_records id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cost_records ALTER COLUMN id SET DEFAULT nextval('public.cost_records_id_seq'::regclass);


--
-- Name: events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events ALTER COLUMN id SET DEFAULT nextval('public.events_id_seq'::regclass);


--
-- Name: instances id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instances ALTER COLUMN id SET DEFAULT nextval('public.instances_id_seq'::regclass);


--
-- Name: machine_types id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_types ALTER COLUMN id SET DEFAULT nextval('public.machine_types_id_seq'::regclass);


--
-- Name: machines id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machines ALTER COLUMN id SET DEFAULT nextval('public.machines_id_seq'::regclass);


--
-- Name: spot_prices id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spot_prices ALTER COLUMN id SET DEFAULT nextval('public.spot_prices_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: cost_records cost_records_instance_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cost_records
    ADD CONSTRAINT cost_records_instance_id_key UNIQUE (instance_id);


--
-- Name: cost_records cost_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cost_records
    ADD CONSTRAINT cost_records_pkey PRIMARY KEY (id);


--
-- Name: events events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);


--
-- Name: instances instances_machine_type_id_provider_instance_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instances
    ADD CONSTRAINT instances_machine_type_id_provider_instance_id_key UNIQUE (machine_type_id, provider_instance_id);


--
-- Name: instances instances_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instances
    ADD CONSTRAINT instances_pkey PRIMARY KEY (id);


--
-- Name: machine_types machine_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_types
    ADD CONSTRAINT machine_types_pkey PRIMARY KEY (id);


--
-- Name: machine_types machine_types_provider_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_types
    ADD CONSTRAINT machine_types_provider_name_key UNIQUE (provider, name);


--
-- Name: machines machines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machines
    ADD CONSTRAINT machines_pkey PRIMARY KEY (id);


--
-- Name: spot_prices spot_prices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spot_prices
    ADD CONSTRAINT spot_prices_pkey PRIMARY KEY (id);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: cost_records cost_records_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cost_records
    ADD CONSTRAINT cost_records_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.instances(id);


--
-- Name: events events_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.instances(id);


--
-- Name: events events_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);


--
-- Name: instances instances_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instances
    ADD CONSTRAINT instances_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machines(id);


--
-- Name: instances instances_machine_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instances
    ADD CONSTRAINT instances_machine_type_id_fkey FOREIGN KEY (machine_type_id) REFERENCES public.machine_types(id);


--
-- Name: machines machines_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machines
    ADD CONSTRAINT machines_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: spot_prices spot_prices_machine_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spot_prices
    ADD CONSTRAINT spot_prices_machine_type_id_fkey FOREIGN KEY (machine_type_id) REFERENCES public.machine_types(id);


--
-- PostgreSQL database dump complete
--

\unrestrict Sz16f0HSK1Kf4AGnnO0mOuc9oAXCHVfFGes78afrayhggwhWoCOxfwRZbT5yLsO

