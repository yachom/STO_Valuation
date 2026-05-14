"""NetworkX 기반 그래프 저장소.

태깅된 뉴스 결과 → 노드/엣지로 적재.
Neo4j 스왑을 고려해 인터페이스는 (upsert_entity, add_relation, search) 단순화.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import networkx as nx


def _eid(entity_type: str, name: str) -> str:
    base = f"{entity_type}::{name.strip().lower()}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]


class KnowledgeGraph:
    def __init__(self) -> None:
        self.g = nx.MultiDiGraph()

    # ----- 적재 -----

    _RESERVED = {"type", "name", "id"}

    def upsert_entity(self, entity_type: str, name: str, **attrs: Any) -> str:
        if not name:
            return ""
        nid = _eid(entity_type, name)
        clean_attrs = {k: v for k, v in attrs.items() if k not in self._RESERVED}
        if self.g.has_node(nid):
            existing = self.g.nodes[nid]
            for k, v in clean_attrs.items():
                existing.setdefault(k, v)
        else:
            self.g.add_node(nid, type=entity_type, name=name, **clean_attrs)
        return nid

    def add_relation(self, src: str, rel: str, dst: str, **attrs: Any) -> None:
        if src and dst and self.g.has_node(src) and self.g.has_node(dst):
            self.g.add_edge(src, dst, key=rel, rel=rel, **attrs)

    def add_article(self, article: dict[str, Any]) -> str:
        link = article.get("link") or article.get("originallink") or article.get("title")
        nid = _eid("NewsArticle", link or "unknown")
        if not self.g.has_node(nid):
            self.g.add_node(
                nid,
                type="NewsArticle",
                name=article.get("title", "")[:120],
                link=link,
                pubDate=article.get("pubDate"),
                query=article.get("query"),
            )
        return nid

    def ingest_tagged(self, asset_context: dict[str, Any], tagged_results: list[dict[str, Any]]) -> dict[str, Any]:
        asset_name = asset_context.get("asset_name") or asset_context.get("road") or "Asset"
        asset_id = self.upsert_entity("Asset", asset_name, **{k: v for k, v in asset_context.items() if v})

        added_articles = 0
        added_entities = 0
        added_rels = 0

        for row in tagged_results:
            tagged = row.get("tagged") or {}
            if tagged.get("asset_relevance") in ("none", None):
                continue
            article = row.get("article", {})
            article_id = self.add_article(article)
            added_articles += 1

            local_to_global: dict[str, str] = {}
            for ent in tagged.get("entities", []) or []:
                etype = ent.get("type") or "Entity"
                ename = ent.get("name")
                if not ename:
                    continue
                gid = self.upsert_entity(etype, ename, **(ent.get("attrs") or {}))
                local_to_global[ent.get("id", ename)] = gid
                self.add_relation(gid, "MENTIONED_IN", article_id, evidence=tagged.get("summary_ko"))
                added_entities += 1

            for rel in tagged.get("relations", []) or []:
                src = local_to_global.get(rel.get("src"))
                dst = local_to_global.get(rel.get("dst"))
                if src and dst:
                    self.add_relation(src, rel.get("rel", "RELATED_TO"), dst, evidence=rel.get("evidence"))
                    added_rels += 1

            for sig in tagged.get("axis_signals", []) or []:
                axis = sig.get("axis")
                if not axis:
                    continue
                axis_id = self.upsert_entity("Axis", axis)
                self.add_relation(
                    asset_id,
                    "AFFECTS_AXIS",
                    axis_id,
                    polarity=sig.get("polarity"),
                    strength=sig.get("strength"),
                    note=sig.get("note"),
                    article=article_id,
                )
                added_rels += 1

        return {
            "asset_id": asset_id,
            "added_articles": added_articles,
            "added_entities": added_entities,
            "added_relations": added_rels,
            "nodes": self.g.number_of_nodes(),
            "edges": self.g.number_of_edges(),
        }

    # ----- 조회 -----

    def axis_evidence(self, axis: str, top_k: int = 15) -> list[dict[str, Any]]:
        """축에 영향을 주는 사건/이슈 목록 (article 메타 포함)."""
        out: list[dict[str, Any]] = []
        for u, v, k, data in self.g.edges(keys=True, data=True):
            if data.get("rel") == "AFFECTS_AXIS" and self.g.nodes[v].get("name") == axis:
                article_node = self.g.nodes.get(data.get("article"), {})
                out.append(
                    {
                        "asset": self.g.nodes[u].get("name"),
                        "axis": axis,
                        "polarity": data.get("polarity"),
                        "strength": data.get("strength"),
                        "note": data.get("note"),
                        "article": {
                            "title": article_node.get("name"),
                            "link": article_node.get("link"),
                            "pubDate": article_node.get("pubDate"),
                        },
                    }
                )
        out.sort(key=lambda x: (x.get("strength") or 0), reverse=True)
        return out[:top_k]

    def search_entities(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        q = query.lower().strip()
        results = []
        for nid, data in self.g.nodes(data=True):
            name = (data.get("name") or "").lower()
            if q in name:
                results.append({"id": nid, **data})
        return results[:top_k]

    def neighbors(self, entity_name: str, max_hops: int = 1) -> dict[str, Any]:
        target = None
        for nid, data in self.g.nodes(data=True):
            if (data.get("name") or "").lower() == entity_name.lower():
                target = nid
                break
        if not target:
            return {"found": False}
        edges = []
        for u, v, k, data in self.g.out_edges(target, keys=True, data=True):
            edges.append({"direction": "out", "rel": data.get("rel"), "target": self.g.nodes[v].get("name")})
        for u, v, k, data in self.g.in_edges(target, keys=True, data=True):
            edges.append({"direction": "in", "rel": data.get("rel"), "source": self.g.nodes[u].get("name")})
        return {"found": True, "entity": self.g.nodes[target], "edges": edges[:50]}

    def summary(self) -> dict[str, Any]:
        type_counts: dict[str, int] = {}
        for _, data in self.g.nodes(data=True):
            t = data.get("type") or "Unknown"
            type_counts[t] = type_counts.get(t, 0) + 1
        rel_counts: dict[str, int] = {}
        for _, _, data in self.g.edges(data=True):
            r = data.get("rel") or "REL"
            rel_counts[r] = rel_counts.get(r, 0) + 1
        return {
            "nodes": self.g.number_of_nodes(),
            "edges": self.g.number_of_edges(),
            "node_types": type_counts,
            "relation_types": rel_counts,
        }

    # ----- 영속화 -----

    def save(self, path: str | Path) -> None:
        data = nx.readwrite.json_graph.node_link_data(self.g, edges="links")
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, path: str | Path) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.g = nx.readwrite.json_graph.node_link_graph(data, directed=True, multigraph=True, edges="links")
