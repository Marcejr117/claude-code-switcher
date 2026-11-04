"""Guided UI regressions: temporary profiles, no provider traffic or logins."""
import contextlib
import io
import unittest
from unittest.mock import patch

import test_csw

csw = test_csw.csw
ui = csw.ui_module()
hot = csw.hot_module()


class UiTest(unittest.TestCase):
    def setUp(self):
        test_csw.ProfilesTest.setUp(self)
        self.output = io.StringIO()
        self.enter(contextlib.redirect_stdout(self.output))
        self.enter(patch.object(csw, "hot_module", return_value=hot))
        self.enter(patch.object(csw, "ui_module", return_value=ui))
        self.enter(patch.object(hot, "sessions", return_value=[]))
        self.catalog = {"source": "Synthetic catalog", "models": [
            {"model": f"gpt-test-{i:02}", "label": f"Model {i}"} for i in range(1, 33)
        ]}

    def enter(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def picker(self, answers):
        with patch("builtins.input", side_effect=answers):
            return ui.pick_model(csw, csw.resolve("codex"), self.proxy, self.catalog)

    def test_help_variants_never_access_accounts_or_network(self):
        with patch.object(csw, "read_json", side_effect=AssertionError("read")), patch.object(csw, "model_catalog", side_effect=AssertionError("network")):
            for args in (["help"], ["--help"], ["-h"], ["help", "--all"], ["hot", "help"], ["hot", "--help"]):
                self.assertEqual(csw.main(args), 0)
        result = self.output.getvalue()
        for command in ("csw hot start", "csw model", "csw restore <id-copia>", "csw monitor", "csw save", "/model", "goals"):
            self.assertIn(command, result)

    def test_help_rejects_unknown_flag(self):
        with self.assertRaisesRegex(ValueError, "csw help"):
            csw.main(["help", "--unknown"])

    def test_home_cancel_does_not_change_profiles(self):
        before = csw.LIVE.read_bytes()
        with patch("builtins.input", return_value="0"), patch.object(hot, "main") as launch:
            self.assertEqual(ui.home(csw), 0)
        launch.assert_not_called()
        self.assertEqual(before, csw.LIVE.read_bytes())
        self.assertIn("¿QUÉ QUIERES HACER?", self.output.getvalue())

    def test_home_changes_settings_without_ever_launching_claude(self):
        with patch("builtins.input", side_effect=["1", "1", "1", "s"]), patch.object(hot, "main") as launch, patch.object(csw, "switch") as change:
            self.assertEqual(ui.home(csw), 0)
        launch.assert_not_called()
        change.assert_called_once_with("codex")

    def test_home_global_change_confirmation_defaults_no(self):
        with patch("builtins.input", side_effect=["1", "1", "1", "", "0"]), patch.object(hot, "main") as launch, patch.object(csw, "switch") as change:
            self.assertEqual(ui.home(csw), 0)
        launch.assert_not_called()
        change.assert_not_called()

    def test_home_hot_switch_is_explicit_even_when_session_exists(self):
        hot.sessions.return_value = [(None, {})]
        with patch("builtins.input", return_value="2"), patch.object(hot, "main", return_value=0) as change:
            self.assertEqual(ui.home(csw), 0)
        self.assertEqual(change.call_args.args[1], ["menu"])

    def test_no_hot_session_explains_how_to_open_one(self):
        with patch("builtins.input", side_effect=["2", "0"]), patch.object(hot, "main") as change:
            ui.home(csw)
        change.assert_not_called()
        self.assertIn("no necesitas abrir otra sesión", self.output.getvalue())

    def test_profile_picker_can_go_back_to_providers(self):
        with patch("builtins.input", side_effect=["1", "b", "2", "1"]):
            self.assertEqual(hot.choose_profile(csw), csw.resolve("claude"))

    def test_profile_picker_can_cancel_at_account_step(self):
        with patch("builtins.input", side_effect=["1", "0"]):
            self.assertIsNone(hot.choose_profile(csw))

    def test_profile_picker_login_filters_api_key_accounts(self):
        with patch("builtins.input", side_effect=["2", "1"]):
            self.assertEqual(hot.choose_profile(csw, allowed=("Claude sub", "Codex", "Cursor")), csw.resolve("claude"))
        self.assertNotIn("DeepSeek", self.output.getvalue())

    def test_number_menu_reprompts_on_invalid_input(self):
        with patch("builtins.input", side_effect=["oops", "99", "1"]):
            self.assertEqual(hot.choose_number("Pick", [("one", "value")]), "value")

    def test_model_picker_first_page_is_bounded(self):
        model, _ = self.picker(["0"])
        self.assertIsNone(model)
        self.assertIn("gpt-test-15", self.output.getvalue())
        self.assertNotIn("gpt-test-16", self.output.getvalue())
        self.assertIn("página 1/3", self.output.getvalue())

    def test_model_picker_next_previous_and_stable_index(self):
        model, _ = self.picker(["n", "p", "n", "16"])
        self.assertEqual(model, "gpt-test-16")
        self.assertIn("página 2/3", self.output.getvalue())

    def test_model_picker_search_case_insensitive_and_keeps_indices(self):
        model, _ = self.picker(["/TEST-22", "1", "22"])
        self.assertEqual(model, "gpt-test-22")
        self.assertIn("1 modelo ·", self.output.getvalue())
        self.assertIn("lista filtrada", self.output.getvalue())

    def test_model_picker_empty_search_can_clear_filter(self):
        model, _ = self.picker(["/absent", "/", "1"])
        self.assertEqual(model, "gpt-test-01")
        self.assertIn("Sin coincidencias", self.output.getvalue())

    def test_model_picker_refresh_uses_updated_catalog(self):
        refreshed = {"source": "New catalog", "models": [{"model": "gpt-new", "label": "New"}]}
        with patch.object(csw, "model_catalog", return_value=refreshed) as refresh:
            model, catalog = self.picker(["r", "1"])
        self.assertEqual(model, "gpt-new")
        self.assertEqual(catalog, refreshed)
        self.assertTrue(refresh.call_args.kwargs["refresh"])

    def test_model_cancel_does_not_write_settings_or_profile(self):
        profile = csw.resolve("codex")
        before = (profile.read_bytes(), csw.LIVE.read_bytes())
        with patch.object(csw.sys.stdin, "isatty", return_value=True), patch.object(csw, "model_catalog", return_value=self.catalog), patch("builtins.input", return_value="q"):
            csw.select_model("codex")
        self.assertEqual(before, (profile.read_bytes(), csw.LIVE.read_bytes()))

    def test_model_selection_saves_exact_filtered_model_only_for_chosen_profile(self):
        before = csw.LIVE.read_bytes()
        with patch.object(csw.sys.stdin, "isatty", return_value=True), patch.object(csw, "model_catalog", return_value=self.catalog), patch("builtins.input", side_effect=["/test-22", "22"]):
            csw.select_model("codex")
        self.assertEqual(csw.read_json(csw.resolve("codex"))["model"], "gpt-test-22")
        self.assertEqual(before, csw.LIVE.read_bytes())

    def test_noninteractive_model_requires_id_before_catalog_request(self):
        with patch.object(csw.sys.stdin, "isatty", return_value=False), patch.object(csw, "model_catalog") as catalog:
            with self.assertRaisesRegex(ValueError, "Indica el modelo"):
                csw.select_model("codex")
        catalog.assert_not_called()

    def test_remove_confirmation_defaults_no(self):
        with patch("builtins.input", side_effect=["5", "", "0"]), patch.object(hot, "choose_profile", return_value=csw.resolve("codex")), patch.object(csw, "main") as action:
            ui.accounts_menu(csw, hot)
        action.assert_not_called()
        self.assertIn("cuenta conservada", self.output.getvalue())

    def test_remove_confirmation_delegates_existing_protected_operation(self):
        with patch("builtins.input", side_effect=["5", "s", "0"]), patch.object(hot, "choose_profile", return_value=csw.resolve("codex")), patch.object(csw, "main") as action:
            ui.accounts_menu(csw, hot)
        action.assert_called_once_with(["remove", "codex"])

    def test_refused_removal_leaves_menu_usable(self):
        with patch("builtins.input", side_effect=["5", "s", "0"]), patch.object(hot, "choose_profile", return_value=csw.resolve("codex")), patch.object(csw, "main", side_effect=ValueError("Cuenta en uso")):
            ui.accounts_menu(csw, hot)
        self.assertIn("Cuenta en uso", self.output.getvalue())

    def test_add_cancel_never_creates_account(self):
        with patch("builtins.input", side_effect=["2", "1", "", "0"]), patch.object(csw, "main") as action:
            ui.accounts_menu(csw, hot)
        action.assert_not_called()

    def test_account_menu_renews_codex_without_browser_option(self):
        with patch("builtins.input", side_effect=["3", "0"]), patch.object(hot, "choose_profile", return_value=csw.resolve("codex")), patch.object(csw, "main") as action:
            ui.accounts_menu(csw, hot)
        action.assert_called_once_with(["login", "codex"])
        self.assertIn("código de un solo uso", self.output.getvalue())

    def test_default_confirmation_does_not_switch_by_accident(self):
        with patch("builtins.input", side_effect=["7", "", "0"]), patch.object(hot, "choose_profile", return_value=csw.resolve("codex")), patch.object(csw, "switch") as change:
            ui.accounts_menu(csw, hot)
        change.assert_not_called()

    def test_restore_menu_ignores_already_restored_archives_and_symlinks(self):
        root = csw.ROOT / "removed-accounts"
        for name in ("removed", "restored"):
            directory = root / name
            directory.mkdir(parents=True)
            csw.write_json(directory / "metadata.json", {"label": name})
        csw.write_json(root / "removed/profile.json", self.proxy)
        (root / "link").symlink_to(root / "removed", target_is_directory=True)
        with patch("builtins.input", side_effect=["1", "s"]), patch.object(csw, "main") as action:
            ui.restore_menu(csw, hot)
        action.assert_called_once_with(["restore", "removed"])
        self.assertNotIn("copia restored", self.output.getvalue())
        self.assertNotIn("copia link", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
