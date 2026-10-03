"""External-style streaming YAML source: uv run --with pyyaml mic inspect MODULE:DATASET."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import mic


@dataclass(frozen=True)
class YamlDocuments(mic.DatasetSource):
    """One case per YAML document, separated by ---; not one giant YAML list."""

    path: Path

    def read(self, ctx: mic.ReadContext) -> Iterator[object]:
        # An external source needs only public Mic imports. The optional parser
        # is imported when reading, not when discovering evaluation definitions.
        import yaml

        class Loader(yaml.SafeLoader):
            depth = 0
            nodes = 0

            def compose_node(self, parent, index):
                if self.check_event(yaml.AliasEvent):
                    raise yaml.YAMLError("Aliases are not supported")
                self.depth += 1
                self.nodes += 1
                if self.depth > 64 or self.nodes > 10_000:
                    raise yaml.YAMLError("Document structure exceeds parser limits")
                try:
                    return super().compose_node(parent, index)
                finally:
                    self.depth -= 1

            def construct_mapping(self, node, deep=False):
                keys = set()
                for key_node, _ in node.value:
                    key = self.construct_object(key_node, deep=deep)
                    if not isinstance(key, str) or key in keys:
                        raise yaml.YAMLError("Mapping keys must be unique strings")
                    keys.add(key)
                return super().construct_mapping(node, deep=deep)

        ctx.set_provenance(provider="yaml", path=str(self.path))
        with self.path.open("rb") as stream:
            loader = Loader(stream)
            try:
                while loader.check_data():
                    loader.nodes = 0
                    yield loader.get_data()
            except yaml.YAMLError:
                # Parser messages can include source excerpts; do not expose them.
                raise mic.DatasetError("Invalid or unsupported YAML document stream") from None
            finally:
                loader.dispose()
