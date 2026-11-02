"""Prove the author path against real Hermes code. Skipped unless run inside a Hermes install.

Run it with the Hermes interpreter, from the Hermes checkout. Do not run it from the plugin folder:
the plugin's top-level ``tools.py`` would shadow Hermes's ``tools`` package and the imports would fail.

    cd <hermes-checkout>
    TEAM_MEMORY_REAL_HERMES=1 <hermes-venv>/bin/python -m unittest discover -s <plugin-dir>/tests -p 'test_hermes_integration.py'

It starts its own fake backends on loopback and a temporary config. It does not touch a real
gateway, Qdrant, Telegram or the model. It covers the real session context, the real
thread-propagation helper tool batches use, and the real tool registry dispatch. The end-to-end
check in the README ("Verify after install") covers the rest.
"""

import asyncio
import contextvars
import json
import threading
import unittest

from support import PluginTestCase, bind_session, load_plugin, plugin_module, using_real_hermes


@unittest.skipUnless(using_real_hermes(), "set TEAM_MEMORY_REAL_HERMES=1 and use the Hermes interpreter")
class RealHermesAuthorTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        from tools.registry import registry

        self.registry = registry
        schemas = plugin_module("schemas")
        registry.register(name="team_share", toolset="team_memory", schema=schemas.TEAM_SHARE,
                          handler=self.tools.team_share, check_fn=self.tools.is_available, override=True)

    def test_session_module_has_what_the_plugin_needs(self):
        from gateway.session_context import get_session_env, session_context_engaged

        bind_session(user_id="user-1", user_name="Ada")
        self.assertTrue(session_context_engaged())
        self.assertEqual(get_session_env("HERMES_SESSION_USER_ID"), "user-1")
        self.assertEqual(get_session_env("HERMES_SESSION_USER_NAME"), "Ada")

    def test_registry_dispatch_in_tool_worker_thread_sees_each_sender(self):
        from tools.thread_context import propagate_context_to_thread

        registry = self.registry

        async def one_message(user_id):
            bind_session(user_id=user_id, user_name=user_id)
            # The gateway hands the turn to an executor with copy_context, and the tool executor
            # then starts the tool in a worker thread with propagate_context_to_thread.
            def turn():
                def tool_worker():
                    return registry.dispatch(
                        "team_share", {"text": f"from {user_id}", "user_id": "spoofed", "author": "spoofed"})

                holder = {}
                wrapped = propagate_context_to_thread(lambda: holder.update(result=tool_worker()))
                thread = threading.Thread(target=wrapped)
                thread.start()
                thread.join()
                return holder["result"]

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, contextvars.copy_context().run, turn)

        async def run_all(users):
            # create_task copies the context, like one task per inbound message in the gateway.
            return await asyncio.gather(*(asyncio.create_task(one_message(user)) for user in users))

        users = [f"user-{i}" for i in range(8)]
        results = asyncio.run(run_all(users))
        for result in results:
            self.assertEqual(json.loads(result).get("status"), "shared", result)
        stored = {p["payload"]["text"]: p["payload"]["author_id"] for p in self.backend.stored_points()}
        self.assertEqual(stored, {f"from {user}": f"telegram:{user}" for user in users})

    def test_plugin_loads_against_real_modules(self):
        self.assertTrue(callable(load_plugin().register))


if __name__ == "__main__":
    unittest.main()
