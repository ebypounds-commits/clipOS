from clipos_api.modules import EXPECTED_MODULES, BoundedModule


def test_exactly_21_modules_are_present() -> None:
    assert len(EXPECTED_MODULES) == 21
    assert BoundedModule.IDENTITY.value == "identity"
    assert BoundedModule.NOTIFICATIONS.value == "notifications"
