import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import MagicMock, call, patch

from agent_manage import container_runtime


class ContainerRuntimeTest(unittest.TestCase):
    def _valid_config(self, public_root: str, runtime_root: str, port: int = 8080) -> str:
        return (
            f"pid {runtime_root}/nginx.pid;\n"
            f"error_log {runtime_root}/log/error.log warn;\n"
            "http {\n"
            "include /etc/nginx/mime.types;\n"
            f"access_log {runtime_root}/log/access.log;\n"
            f"client_body_temp_path {runtime_root}/client_temp;\n"
            f"proxy_temp_path {runtime_root}/proxy_temp;\n"
            f"fastcgi_temp_path {runtime_root}/fastcgi_temp;\n"
            f"uwsgi_temp_path {runtime_root}/uwsgi_temp;\n"
            f"scgi_temp_path {runtime_root}/scgi_temp;\n"
            "server {\n"
            f"listen {port};\n"
            f"root {public_root};\n"
            "autoindex off;\n"
            "disable_symlinks on;\n"
            "}\n"
            "}\n"
        )

    def test_container_dispatches_start_hooks_after_launching_services(self):
        import os
        import threading
        started = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "openclaw.json"
            config.write_text("{}")
            nginx, gateway = MagicMock(), MagicMock()
            nginx.poll.return_value = None
            def gateway_poll():
                self.assertTrue(started.wait(2))
                return 0
            gateway.poll.side_effect = gateway_poll
            def restore(path, **kwargs):
                self.assertEqual(path, config)
                started.set()
                return {"ok": True, "agents": []}
            with patch.dict(os.environ, {"OPENCLAW_CONFIG_PATH": str(config)}), \
                 patch.object(container_runtime, "select_nginx_config", return_value=Path(tmp)/"nginx.conf"), \
                 patch.object(container_runtime.subprocess, "Popen", side_effect=[nginx, gateway]), \
                 patch.object(container_runtime.signal, "signal"), \
                 patch.object(container_runtime, "start_agents", side_effect=restore) as hooks:
                self.assertEqual(container_runtime.run_container(["openclaw"], 80), 0)
            hooks.assert_called_once()

    def test_resolve_nginx_port_defaults_to_port_80(self):
        self.assertEqual(container_runtime.resolve_nginx_port(None), 80)
        self.assertEqual(container_runtime.resolve_nginx_port("8080"), 8080)

    def test_resolve_nginx_port_rejects_invalid_values(self):
        for value in ("nope", "0", "65536"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                container_runtime.resolve_nginx_port(value)

    def test_validate_nginx_config_does_not_apply_a_network_directive_or_proxy_target_blacklist(self):
        valid = self._valid_config(
            "/home/node/.openclaw/workspace/public",
            "/tmp/nginx",
        )

        config = valid.replace(
            "disable_symlinks on;",
            "disable_symlinks on;\n"
            "location /api/ { proxy_pass http://127.0.0.1:8788; }\n"
            "location /public/ { proxy_pass https://example.com/api/; }\n"
            "location /dynamic/ { proxy_pass $backend; }\n"
            "location /named/ { proxy_pass http://backend; }\n"
            "location /auth/ { auth_request /authorize; }\n"
            "location /fcgi/ { fastcgi_pass application:9000; }",
        )
        container_runtime.validate_nginx_config(config, 8080)

    def test_validate_nginx_config_preserves_platform_constraints(self):
        valid = self._valid_config(
            "/home/node/.openclaw/workspace/public",
            "/tmp/nginx",
        )
        invalid_configs = (
            valid.replace("include /etc/nginx/mime.types;", "include /etc/nginx/conf.d/*.conf;"),
            valid.replace("workspace/public", "workspace"),
            valid.replace("listen 8080;", "listen 8081;"),
            valid.replace("/tmp/nginx/proxy_temp", "/tmp/proxy_temp"),
            valid.replace("autoindex off;", "autoindex on;"),
            valid.replace("disable_symlinks on;", "disable_symlinks off;"),
            valid.replace(
                "disable_symlinks on;",
                "disable_symlinks on;\n"
                "location /private/ { alias /home/node/.openclaw/; }",
            ),
            valid.replace(
                "disable_symlinks on;",
                "disable_symlinks on;\n"
                "location / { root /home/node/.openclaw; try_files $uri $uri/ =404; }",
            ),
        )
        container_runtime.validate_nginx_config(valid, 8080)
        for invalid in invalid_configs:
            with self.subTest(config=invalid), self.assertRaises(ValueError):
                container_runtime.validate_nginx_config(invalid, 8080)

    def test_initialize_preserves_existing_platform_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "state/nginx/nginx.conf"
            template_path = root / "image/nginx.conf"
            public_root = root / "state/workspace/public"
            runtime_root = root / "runtime"
            template_path.parent.mkdir(parents=True)
            runtime_config_path = runtime_root.as_posix()
            public_config_path = public_root.as_posix()
            template_path.write_text(
                self._valid_config(public_config_path, runtime_config_path).replace(
                    "listen 8080;",
                    "listen __UNITAG_NGINX_PORT__;",
                ),
                encoding="utf-8",
            )
            with (
                patch.object(container_runtime, "NGINX_CONFIG_PATH", config_path),
                patch.object(container_runtime, "NGINX_CONFIG_TEMPLATE", template_path),
                patch.object(container_runtime, "PUBLIC_ROOT", public_root),
                patch.object(container_runtime, "NGINX_RUNTIME_ROOT", runtime_root),
            ):
                container_runtime.initialize_nginx_config(8080)
                first = config_path.read_text(encoding="utf-8")
                container_runtime.initialize_nginx_config(8080)
                self.assertEqual(config_path.read_text(encoding="utf-8"), first)
                self.assertTrue(public_root.is_dir())

    def test_invalid_persistent_config_uses_fallback_without_overwriting_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "state/nginx/nginx.conf"
            template_path = root / "image/nginx.conf"
            public_root = root / "state/workspace/public"
            runtime_root = root / "runtime"
            config_path.parent.mkdir(parents=True)
            template_path.parent.mkdir(parents=True)
            valid = self._valid_config(public_root.as_posix(), runtime_root.as_posix())
            invalid = valid.replace(
                "disable_symlinks on;",
                "disable_symlinks on;\n"
                "location / { root /home/node/.openclaw; try_files $uri $uri/ =404; }",
            )
            config_path.write_text(invalid, encoding="utf-8")
            template_path.write_text(
                valid.replace("listen 8080;", "listen __UNITAG_NGINX_PORT__;"),
                encoding="utf-8",
            )
            stderr = StringIO()

            with (
                patch.object(container_runtime, "NGINX_CONFIG_PATH", config_path),
                patch.object(container_runtime, "NGINX_CONFIG_TEMPLATE", template_path),
                patch.object(container_runtime, "PUBLIC_ROOT", public_root),
                patch.object(container_runtime, "NGINX_RUNTIME_ROOT", runtime_root),
                patch.object(container_runtime.subprocess, "run") as run,
                redirect_stderr(stderr),
            ):
                selected = container_runtime.select_nginx_config(8080)

            self.assertEqual(selected, runtime_root / "fallback-nginx.conf")
            self.assertEqual(config_path.read_text(encoding="utf-8"), invalid)
            self.assertNotIn("root /home/node/.openclaw;", selected.read_text(encoding="utf-8"))
            self.assertIn("trusted runtime fallback", stderr.getvalue())
            run.assert_called_once_with(
                ["nginx", "-t", "-p", f"{runtime_root}/", "-c", str(selected)],
                check=True,
            )

    def test_nginx_test_failure_uses_fallback_without_overwriting_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "state/nginx/nginx.conf"
            template_path = root / "image/nginx.conf"
            public_root = root / "state/workspace/public"
            runtime_root = root / "runtime"
            config_path.parent.mkdir(parents=True)
            template_path.parent.mkdir(parents=True)
            valid = self._valid_config(public_root.as_posix(), runtime_root.as_posix())
            config_path.write_text(valid, encoding="utf-8")
            template_path.write_text(
                valid.replace("listen 8080;", "listen __UNITAG_NGINX_PORT__;"),
                encoding="utf-8",
            )

            with (
                patch.object(container_runtime, "NGINX_CONFIG_PATH", config_path),
                patch.object(container_runtime, "NGINX_CONFIG_TEMPLATE", template_path),
                patch.object(container_runtime, "PUBLIC_ROOT", public_root),
                patch.object(container_runtime, "NGINX_RUNTIME_ROOT", runtime_root),
                patch.object(
                    container_runtime.subprocess,
                    "run",
                    side_effect=[CalledProcessError(1, ["nginx", "-t"]), None],
                ) as run,
                redirect_stderr(StringIO()),
            ):
                selected = container_runtime.select_nginx_config(8080)

            self.assertEqual(selected, runtime_root / "fallback-nginx.conf")
            self.assertEqual(config_path.read_text(encoding="utf-8"), valid)
            self.assertEqual(run.call_count, 2)

    def test_fallback_validation_failure_is_not_suppressed(self):
        with (
            patch.object(container_runtime, "initialize_nginx_config", side_effect=ValueError("user config")),
            patch.object(container_runtime, "_render_fallback_nginx_config", return_value=Path("fallback.conf")),
            patch.object(
                container_runtime,
                "_test_nginx_config",
                side_effect=CalledProcessError(1, ["nginx", "-t"]),
            ),
            redirect_stderr(StringIO()),
            self.assertRaises(CalledProcessError),
        ):
            container_runtime.select_nginx_config(8080)

    def test_run_container_starts_both_processes_and_terminates_peer(self):
        config_path = Path("/tmp/nginx/fallback-nginx.conf")
        nginx = MagicMock()
        nginx.poll.side_effect = [None, None]
        openclaw = MagicMock()
        openclaw.poll.return_value = 7

        with (
            patch.object(container_runtime, "select_nginx_config", return_value=config_path),
            patch.object(container_runtime.subprocess, "Popen", side_effect=[nginx, openclaw]) as popen,
            patch.object(container_runtime.signal, "signal"),
        ):
            result = container_runtime.run_container(["openclaw", "gateway", "run"], 8080)

        self.assertEqual(result, 7)
        self.assertEqual(
            popen.call_args_list,
            [
                call(
                    [
                        "nginx",
                        "-p",
                        f"{container_runtime.NGINX_RUNTIME_ROOT}/",
                        "-c",
                        str(config_path),
                        "-g",
                        "daemon off;",
                    ]
                ),
                call(["openclaw", "gateway", "run"]),
            ],
        )
        nginx.terminate.assert_called_once_with()
        nginx.wait.assert_called_once_with(timeout=10.0)
        openclaw.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
