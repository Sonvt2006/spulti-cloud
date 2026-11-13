"""Optional browser integration test with SQLite and no cloud/Redis operations.

Run from repository root with PYTHONPATH=control-plane:
  python control-plane/tests/dashboard_browser.py
Requires playwright and an installed Chrome, or DASHBOARD_BROWSER executable.
Screenshots are written to the ignored .local/dashboard-preview directory.
"""

import json
import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("JWT_SECRET", "isolated-browser-test-only-secret-123456789")

import uvicorn  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AgentJob,
    Instance,
    InstanceAttempt,
    MachineJob,
    MachineLifecycle,
    RecoveryRun,
    SpotPrice,
    SpotRisk,
)
from app.security import hash_password  # noqa: E402
from app.services.instance_manager import MachineRequest, create_machine  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from sqlalchemy import create_engine, delete, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from test_instance_manager import MODELS, PROFILES, seed_manager  # noqa: E402
from test_ssh_provisioning import public_key  # noqa: E402


def seed(factory):
    now = datetime.now(UTC)
    with factory() as db:
        owner, _ = seed_manager(db)
        owner.name = "Minh Long"
        owner.password_hash = hash_password("local-dashboard-test-only")
        db.execute(delete(SpotPrice))
        for mid, region in ((1, "ap-southeast-1"), (2, "asia-southeast1")):
            for day in range(7):
                for kind, rate in (
                    ("SPOT", Decimal("0.07") + Decimal(day % 3) / 100),
                    ("ON_DEMAND", Decimal("0.384")),
                ):
                    db.add(
                        SpotPrice(
                            machine_type_id=mid,
                            region=region,
                            zone=None,
                            price_type=kind,
                            price_per_hour=rate,
                            recorded_at=now - timedelta(days=7 - day),
                        )
                    )
            db.add(
                SpotRisk(
                    machine_type_id=mid,
                    region=region,
                    zone=None,
                    source="preview-fixture",
                    metric="test-index",
                    rate_min=Decimal("0.05"),
                    rate_max=Decimal("0.1"),
                    observed_at=now,
                )
            )
        db.commit()
        for index, name in enumerate(
            ("training-worker-01", "data-pipeline-02", "inference-dev-03")
        ):
            machine = create_machine(
                db, owner.id, MachineRequest(name=name, vcpu=2, ram_gib=4), str(uuid4())
            )
            attempt = db.scalar(
                select(InstanceAttempt)
                .where(InstanceAttempt.machine_id == machine.id)
                .order_by(InstanceAttempt.position)
            )
            instance = Instance(
                machine_id=machine.id,
                machine_type_id=attempt.machine_type_id,
                provider_instance_id=f"i-preview-{index}",
                region=attempt.region,
                zone=attempt.zone,
                started_at=now - timedelta(days=5 - index),
            )
            db.add(instance)
            db.flush()
            attempt.instance_id = instance.id
            attempt.instance_ref = instance.provider_instance_id
            attempt.public_ip = f"203.0.113.{20 + index}"
            attempt.provider_status = "running"
            attempt.state = "ACTIVE"
            machine.status = "RUNNING" if index != 2 else "RECOVERING"
            life = db.get(MachineLifecycle, machine.id)
            life.checked_at = now
            if index == 0:
                job = AgentJob(
                    id=str(uuid4()),
                    user_id=owner.id,
                    agent_token_hash="fixture",
                    total_steps=1000,
                    current_step=650,
                    checkpoint_step=600,
                    checkpoint_every=100,
                    heartbeat_every=25,
                    status="RUNNING",
                    last_heartbeat_at=now,
                    checkpoint_data={
                        "storage": "s3",
                        "bucket": "spulti-checkpoints-preview",
                        "key": "jobs/training/checkpoint.json",
                    },
                )
                db.add(job)
                db.flush()
                db.add(MachineJob(machine_id=machine.id, job_id=job.id))
            if index == 2:
                db.add(
                    RecoveryRun(
                        machine_id=machine.id,
                        failed_attempt_id=attempt.id,
                        reason="spot_interruption",
                        state="CLEANUP",
                        started_at=now - timedelta(minutes=2),
                        excluded_until=now + timedelta(minutes=3),
                    )
                )
            db.commit()


def run_browser(base_url, factory, output):
    errors = []
    with sync_playwright() as p:
        executable = os.getenv("DASHBOARD_BROWSER")
        kwargs = (
            {"executable_path": executable} if executable else {"channel": "chrome"}
        )
        browser = p.chromium.launch(headless=True, **kwargs)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, reduced_motion="reduce"
        )
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                errors.append(message.text)
                if "Content Security Policy" in message.text
                else None
            ),
        )
        page.goto(base_url + "/dashboard/")
        expect(page.get_by_role("heading", name="Đăng nhập workspace")).to_be_visible()
        page.get_by_label("Tên đăng nhập hoặc email").fill("owner")
        page.get_by_label("Mật khẩu", exact=True).fill("local-dashboard-test-only")
        page.get_by_role("button", name="Đăng nhập", exact=True).click()
        expect(page.get_by_role("heading", name="Tổng quan workspace")).to_be_visible()
        page.locator(".skip-link").focus()
        page.keyboard.press("Enter")
        expect(page.get_by_role("heading", name="Tổng quan workspace")).to_be_visible()
        assert page.evaluate("document.activeElement.id") == "content"
        expect(page.locator(".machine-table tbody tr")).to_have_count(3)
        page.screenshot(path=str(output / "overview-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(output / "overview-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.get_by_role("link", name="Danh sách máy", exact=True).click()
        page.get_by_label("Tìm theo tên máy").fill("training")
        page.get_by_role("button", name="Lọc", exact=True).click()
        expect(page.locator(".machine-table tbody tr")).to_have_count(1)
        page.get_by_role("link", name="training-worker-01", exact=False).first.click()
        expect(page.get_by_role("heading", name="Job & checkpoint")).to_be_visible()
        expect(page.locator("#content")).to_contain_text(
            "s3://spulti-checkpoints-preview/"
        )
        page.screenshot(path=str(output / "machine-detail-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(output / "machine-detail-mobile.png"), full_page=True)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth"
        ), "Mobile overflow"
        page.get_by_role("button", name="Mở menu").click()
        page.get_by_role("link", name="Tạo máy mới", exact=False).first.click()
        expect(
            page.get_by_role("heading", name="Bạn cần bao nhiêu tài nguyên?")
        ).to_be_visible()
        page.get_by_label("Tên máy", exact=True).fill("browser-created")
        provider = page.locator('#requirements-form select[name="provider"]')
        region = page.locator('#requirements-form select[name="region"]')
        zone = page.locator('#requirements-form select[name="zone"]')
        expect(region).to_be_disabled()
        expect(zone).to_be_disabled()
        for cloud, cloud_region, cloud_zone in (
            ("AWS", "ap-southeast-1", "ap-southeast-1a"),
            ("GCP", "asia-southeast1", "asia-southeast1-b"),
        ):
            provider.select_option(cloud)
            expect(region).to_be_enabled()
            expect(region).to_have_value("")
            expect(zone).to_be_disabled()
            expect(zone).to_have_value("")
            assert region.locator("option").evaluate_all(
                "options => options.map(option => option.value)"
            ) == ["", cloud_region]
            region.select_option(cloud_region)
            expect(zone).to_be_enabled()
            assert zone.locator("option").evaluate_all(
                "options => options.map(option => option.value)"
            ) == ["", cloud_zone]
            zone.select_option(cloud_zone)
            region.select_option("")
            expect(zone).to_be_disabled()
            expect(zone).to_have_value("")
            region.select_option(cloud_region)
            zone.select_option(cloud_zone)
        page.get_by_role("button", name="Tìm máy phù hợp").click()
        expect(page.locator(".candidate")).to_have_count(1)
        page.get_by_role("button", name="Sửa nhu cầu").click()
        expect(provider).to_have_value("GCP")
        expect(region).to_have_value("asia-southeast1")
        expect(zone).to_have_value("asia-southeast1-b")
        expect(zone).to_be_enabled()
        provider.select_option("")
        expect(region).to_be_disabled()
        expect(region).to_have_value("")
        expect(zone).to_be_disabled()
        expect(zone).to_have_value("")
        page.get_by_role("button", name="Tìm máy phù hợp").click()
        expect(page.locator(".candidate")).to_have_count(2)
        page.locator(".candidate").filter(has_text="e2-standard-4").click()
        page.screenshot(path=str(output / "recommendations-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.screenshot(
            path=str(output / "recommendations-desktop.png"), full_page=True
        )
        page.get_by_role("button", name="Tiếp tục", exact=True).click()
        expect(page.get_by_role("heading", name="Xác nhận cấu hình")).to_be_visible()

        keys = []

        def lose_first_response(route):
            if route.request.method != "POST":
                route.continue_()
                return
            keys.append(route.request.headers.get("idempotency-key"))
            response = route.fetch()
            assert response.status == 202, response.text()
            if len(keys) == 1:
                route.abort("failed")
            else:
                route.fulfill(response=response)

        page.route("**/api/machines", lose_first_response)
        page.get_by_role("button", name="Tạo máy Spot").click()
        expect(
            page.get_by_role("heading", name="Xác minh yêu cầu tạo máy")
        ).to_be_visible()
        page.reload()
        page.get_by_role("button", name="Kiểm tra / gửi lại yêu cầu").click()
        page.wait_for_url("**/#/machines/*")
        expect(
            page.get_by_role("heading", name="browser-created", exact=True)
        ).to_be_visible()
        assert len(keys) == 2 and keys[0] == keys[1], "Idempotency key changed"
        machine_id = int(page.url.rsplit("/", 1)[1])
        with factory() as db:
            first = db.scalar(
                select(InstanceAttempt)
                .where(InstanceAttempt.machine_id == machine_id)
                .order_by(InstanceAttempt.position)
            )
            assert first.provider == "GCP", "Selected candidate was not prioritized"
        page.get_by_role("button", name="Xóa máy", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Hủy", exact=True).click()
        page.get_by_role("button", name="Xóa máy", exact=True).click()
        page.get_by_role("dialog").get_by_role(
            "button", name="Xóa máy", exact=True
        ).click()
        expect(page.locator(".detail-title")).to_contain_text("Đang yêu cầu xóa")

        page.get_by_role("link", name="SSH public keys").click()
        page.get_by_label("Tên key").fill("browser-key")
        page.get_by_label("SSH public key", exact=True).fill(public_key())
        page.get_by_role("button", name="Thêm key", exact=True).click()
        expect(page.locator(".key-row").filter(has_text="browser-key")).to_be_visible()

        page.get_by_role("link", name="Tạo job mới", exact=True).click()
        expect(
            page.get_by_role("heading", name="Tạo job mới", exact=True)
        ).to_be_visible()
        expect(page.get_by_role("button", name="Upload & Tạo job")).to_be_disabled()
        expect(page.locator('[name="total_steps"]')).to_have_value("1")
        page.locator('[name="total_steps"]').fill("7")
        page.locator('[name="checkpoint_every"]').fill("3")
        page.locator('select[name="provider"]').select_option("GCP")
        page.get_by_role("button", name="Chọn file Python").focus()
        with page.expect_file_chooser() as chooser:
            page.keyboard.press("Enter")
        chooser.value.set_files(
            {
                "name": "browser_training.py",
                "mimeType": "text/x-python",
                "buffer": b"from spulti import state\nstate['value'] = 1\n",
            }
        )
        expect(page.get_by_role("button", name="Upload & Tạo job")).to_be_enabled()
        expect(page.locator('[name="total_steps"]')).to_have_value("7")
        expect(page.locator('[name="checkpoint_every"]')).to_have_value("3")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output / "submit-job-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.screenshot(path=str(output / "submit-job-desktop.png"), full_page=True)
        job_keys = []

        def lose_first_job_response(route):
            job_keys.append(route.request.headers.get("idempotency-key"))
            response = route.fetch()
            assert response.status == 201, response.text()
            if len(job_keys) == 1:
                route.abort("failed")
            else:
                route.fulfill(response=response)

        page.route("**/api/dashboard/jobs", lose_first_job_response)
        page.get_by_role("button", name="Upload & Tạo job").click()
        expect(
            page.get_by_role("heading", name="Xác minh yêu cầu tạo job")
        ).to_be_visible()
        page.reload()
        page.get_by_role("button", name="Kiểm tra / gửi lại job").click()
        page.wait_for_url("**/#/machines/*")
        expect(page.get_by_role("heading", name="Job & checkpoint")).to_be_visible()
        assert len(job_keys) == 2 and job_keys[0] == job_keys[1]
        assert page.evaluate("sessionStorage.getItem('spulti.pending-job')") is None
        with factory() as db:
            bindings = db.scalars(
                select(MachineJob).join(AgentJob).where(AgentJob.total_steps == 7)
            ).all()
            assert len(bindings) == 1, "Lost upload response created a duplicate job"
            attempt = db.scalar(
                select(InstanceAttempt)
                .where(InstanceAttempt.machine_id == bindings[0].machine_id)
                .order_by(InstanceAttempt.position)
            )
            assert attempt.provider == "GCP"
        page.unroute("**/api/dashboard/jobs")

        # Render a hostile machine name as plain text, including error paths.
        page.get_by_role("link", name="Tổng quan", exact=True).click()
        expect(page.get_by_role("heading", name="Tổng quan workspace")).to_be_visible()
        page.route(
            "**/api/dashboard?*",
            lambda route: route.fulfill(
                status=503,
                json={"detail": "Fixture unavailable <img src=x onerror=alert(1)>"},
            ),
        )
        page.get_by_role("button", name="Làm mới dữ liệu").click()
        expect(
            page.get_by_role("heading", name="Chưa tải được dữ liệu")
        ).to_be_visible()
        assert page.locator("#content img").count() == 0
        page.unroute("**/api/dashboard?*")
        page.get_by_role("button", name="Thử lại", exact=True).click()
        expect(page.get_by_role("heading", name="Tổng quan workspace")).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth"
        ), page.evaluate(
            """[...document.querySelectorAll('body *')]
            .filter(e=>e.getBoundingClientRect().right>innerWidth+1)
            .map(e=>[e.tagName,e.className,e.getBoundingClientRect().right]).slice(0,15)"""
        )
        page.route(
            "**/api/dashboard?*",
            lambda route: route.fulfill(status=401, json={"detail": "Expired token"}),
        )
        page.get_by_role("button", name="Làm mới dữ liệu").click()
        expect(page.get_by_role("heading", name="Đăng nhập workspace")).to_be_visible()
        assert page.evaluate("sessionStorage.getItem('spulti.session')") is None
        page.unroute("**/api/dashboard?*")
        page.unroute("**/api/machines")
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.get_by_role("button", name="Đăng ký", exact=True).click()
        page.get_by_label("Họ và tên").fill("New dashboard user")
        page.get_by_label("Email", exact=True).fill("new-browser@example.com")
        page.get_by_label("Tên đăng nhập", exact=True).fill("new-browser")
        page.get_by_label("Mật khẩu", exact=True).fill("local-browser-registration")
        page.get_by_role("button", name="Tạo tài khoản", exact=True).click()
        expect(page.get_by_role("heading", name="Tổng quan workspace")).to_be_visible()
        expect(page.get_by_role("heading", name="Chưa có máy phù hợp")).to_be_visible()
        page.get_by_role("link", name="Tạo job mới", exact=True).click()
        expect(
            page.get_by_role("link", name="Thêm public key", exact=True)
        ).to_be_visible()
        page.locator("#job-file-input").set_input_files(
            {
                "name": "new_user.py",
                "mimeType": "text/x-python",
                "buffer": b"print(1)\n",
            }
        )
        expect(page.locator(".upload-zone-content")).to_contain_text("new_user.py")
        expect(page.get_by_role("button", name="Upload & Tạo job")).to_be_disabled()
        page.get_by_role("link", name="Tạo máy mới", exact=False).first.click()
        page.get_by_label("Tên máy", exact=True).fill("new-account-machine")
        page.get_by_role("button", name="Tìm máy phù hợp").click()
        page.get_by_role("button", name="Tiếp tục", exact=True).click()
        expect(page.get_by_role("button", name="Tạo máy Spot")).to_be_disabled()
        page.get_by_role("link", name="Thêm public key", exact=True).click()
        page.get_by_label("Tên key").fill("onboarding-key")
        page.get_by_label("SSH public key", exact=True).fill(public_key())
        page.get_by_role("button", name="Thêm key", exact=True).click()
        expect(page.locator(".key-row")).to_contain_text("onboarding-key")
        page.get_by_role("link", name="Về tạo máy", exact=True).click()
        expect(page.get_by_role("heading", name="Xác nhận cấu hình")).to_be_visible()
        expect(page.get_by_role("button", name="Tạo máy Spot")).to_be_enabled()
        assert not errors, errors
        context.close()
        browser.close()
    print(
        "Browser checks passed: login, overview, search, detail, mobile, "
        "recommendations,"
    )
    print(
        "preferred candidate, lost-response idempotency, delete, SSH keys, "
        "upload, job retry after lost response, job prerequisites, errors, expiry, CSP."
    )


def main():
    output = Path(__file__).resolve().parents[2] / ".local" / "dashboard-preview"
    output.mkdir(parents=True, exist_ok=True)
    database_path = output / f"fixture-{uuid4().hex}.sqlite"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine, tables=[m.__table__ for m in MODELS])
    factory = sessionmaker(engine, expire_on_commit=False)

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    with (
        patch.dict(
            os.environ,
            {
                "SPULTI_LAUNCH_CONFIG": json.dumps(PROFILES),
                "SPULTI_MAX_MACHINES_PER_USER": "10",
                "SPULTI_PUBLIC_API_URL": "https://api.example.test",
                "SPULTI_AGENT_RELEASE_URL": "https://releases.example.test/agent.tar.gz",
                "SPULTI_AGENT_RELEASE_SHA256": "a" * 64,
            },
        ),
        patch("app.machines._wake_worker"),
        patch("app.dashboard._wake_worker"),
    ):
        seed(factory)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            if not server.started:
                raise RuntimeError("Fixture API did not start")
            run_browser(f"http://127.0.0.1:{port}", factory, output)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            sock.close()
            app.dependency_overrides.clear()
            engine.dispose()
            database_path.unlink(missing_ok=True)
    print(f"Screenshots: {output}")


if __name__ == "__main__":
    main()
