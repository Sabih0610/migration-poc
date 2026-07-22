"""Fakes for read-only Azure SDK clients — Phase 9 tests.

These never contact Azure. Definitions are derived from the local
fixtures so Azure discovery can be proven interface-compatible with
fixture discovery. Resource ids are scoped to the test subscription and
resource group so boundary enforcement is exercised.
"""

from pathlib import Path

from src.fixtures_loader import load_mock_adf_inventory

TEST_SUB = "11111111-1111-1111-1111-111111111111"
TEST_RG = "AzureFabricMigrationPOC"
TEST_DF = "Sabih-df"

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

_KIND = {
    "pipelines": "pipelines",
    "data_flows": "dataflows",
    "datasets": "datasets",
    "linked_services": "linkedservices",
    "triggers": "triggers",
}


def resource_id(kind: str, name: str, sub: str = TEST_SUB, rg: str = TEST_RG) -> str:
    return (
        f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
        f"Microsoft.DataFactory/factories/{TEST_DF}/{kind}/{name}"
    )


def fixture_raw_definitions(sub: str = TEST_SUB, rg: str = TEST_RG) -> dict:
    """Azure-shaped raw definitions (camelCase + id) built from fixtures."""
    inv = load_mock_adf_inventory(FIXTURES)
    collections = {
        "pipelines": inv.pipelines,
        "data_flows": inv.data_flows,
        "datasets": inv.datasets,
        "linked_services": inv.linked_services,
        "triggers": inv.triggers,
    }
    raw: dict[str, list[dict]] = {}
    for key, items in collections.items():
        defs = []
        for item in items:
            data = item.model_dump(by_alias=True, exclude_none=True)
            data["id"] = resource_id(_KIND[key], item.name, sub, rg)
            defs.append(data)
        raw[key] = defs
    return raw


class _FakeItem:
    def __init__(self, definition: dict):
        self._definition = definition
        self.name = definition["name"]
        self.id = definition.get("id")

    def as_dict(self) -> dict:
        return dict(self._definition)


class _FakeOperation:
    def __init__(self, definitions: list[dict], rg: str, df: str):
        self._by_name = {d["name"]: d for d in definitions}
        self._rg = rg
        self._df = df

    def list_by_factory(self, resource_group, factory_name):
        assert resource_group == self._rg and factory_name == self._df
        return [_FakeItem(d) for d in self._by_name.values()]

    def get(self, resource_group, factory_name, name):
        assert resource_group == self._rg and factory_name == self._df
        return _FakeItem(self._by_name[name])


class _FakeFactoriesOp:
    def __init__(self, rg: str, df: str, sub: str):
        self._rg, self._df, self._sub = rg, df, sub

    def get(self, resource_group, factory_name):
        assert resource_group == self._rg and factory_name == self._df
        return type(
            "Factory",
            (),
            {
                "id": (
                    f"/subscriptions/{self._sub}/resourceGroups/{self._rg}/"
                    f"providers/Microsoft.DataFactory/factories/{self._df}"
                ),
                "name": factory_name,
                "location": "northcentralus",
                "provisioning_state": "Succeeded",
            },
        )()


class FakeDataFactoryClient:
    def __init__(self, raw: dict, rg: str = TEST_RG, df: str = TEST_DF, sub: str = TEST_SUB):
        self.pipelines = _FakeOperation(raw["pipelines"], rg, df)
        self.data_flows = _FakeOperation(raw["data_flows"], rg, df)
        self.datasets = _FakeOperation(raw["datasets"], rg, df)
        self.linked_services = _FakeOperation(raw["linked_services"], rg, df)
        self.triggers = _FakeOperation(raw["triggers"], rg, df)
        self.factories = _FakeFactoriesOp(rg, df, sub)


class _FakeResourceGroupsOp:
    def __init__(self, rg: str, sub: str):
        self._rg, self._sub = rg, sub

    def get(self, resource_group_name):
        assert resource_group_name == self._rg
        return type(
            "ResourceGroup",
            (),
            {
                "id": f"/subscriptions/{self._sub}/resourceGroups/{self._rg}",
                "name": self._rg,
                "location": "northcentralus",
            },
        )()


class _FakeProvidersOp:
    def get(self, namespace):
        return type(
            "Provider",
            (),
            {"namespace": namespace, "registration_state": "Registered"},
        )()


class FakeResourceClient:
    def __init__(self, rg: str = TEST_RG, sub: str = TEST_SUB):
        self.resource_groups = _FakeResourceGroupsOp(rg, sub)
        self.providers = _FakeProvidersOp()


def make_client(raw=None, sub=TEST_SUB, rg=TEST_RG, df=TEST_DF, **overrides):
    """Build an AzureADFClient wired to the fakes (no network)."""
    from src.connectors.azure_adf_client import AzureADFClient

    raw = raw if raw is not None else fixture_raw_definitions(sub, rg)
    df_client = FakeDataFactoryClient(raw, rg, df, sub)
    rm_client = FakeResourceClient(rg, sub)
    params = dict(
        tenant_id="tenant",
        client_id="client",
        client_secret="SECRET-should-never-leak",
        subscription_id=sub,
        resource_group=rg,
        data_factory_name=df,
        credential_factory=lambda *a, **k: object(),
        datafactory_client_factory=lambda *a, **k: df_client,
        resource_client_factory=lambda *a, **k: rm_client,
    )
    params.update(overrides)
    return AzureADFClient(**params)
