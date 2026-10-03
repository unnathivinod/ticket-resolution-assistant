"""Endpoints for things that change over time: tickets, knowledge-base articles and ticket classes.

  GET    /v1/taxonomy                          the classes in use                      (any key)
  POST   /v1/taxonomy                          add a class                             (admin key)
  DELETE /v1/taxonomy/{kind}/{name}            retire a class                          (admin key)
  POST   /v1/tickets                           add or update a resolved ticket         (admin key)
  PUT    /v1/kb/{id}                           add or update a knowledge-base article  (admin key)
  DELETE /v1/documents/{id}                    hide a ticket or article from search    (admin key)
  GET    /v1/documents/{id}                    is the search index up to date for it?  (admin key)
  GET    /v1/ingest/status                     how many documents are still waiting?   (admin key)
  GET    /v1/classes/proposals                 new classes suggested by the discovery job
  POST   /v1/classes/proposals/{id}/approve    accept one (this adds the class)
  POST   /v1/classes/proposals/{id}/reject

A write is saved in PostgreSQL first, then a note goes on a queue. The ingestion worker
updates the search index a moment later, so these endpoints answer "202 Accepted" at once.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from prometheus_client import Counter
from pydantic import BaseModel, Field

DOCUMENTS = Counter("gateway_documents_total", "Document changes accepted", ["type", "action"])
CLASSES = Counter("gateway_class_changes_total", "Changes to the list of ticket classes", ["action"])

TICKET_ID = r"^T-[A-Za-z0-9-]{1,40}$"
ARTICLE_ID = r"^KB-[A-Za-z0-9-]{1,40}$"
CLASS_NAME = r"^[a-z][a-z0-9_]{1,48}$"


class TicketIn(BaseModel):
    id: str | None = Field(None, pattern=TICKET_ID, description="Leave empty to get a new ID")
    subject: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=4000, description="The customer's complaint")
    resolution_steps: list[str] = Field(min_length=1, max_length=20, description="What fixed it, in order")
    category: str
    product: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    sentiment: Literal["negative", "neutral", "positive"] | None = None
    scenario_id: str | None = Field(None, max_length=40, description="Optional answer key used by evals")


class ArticleIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=20, max_length=20000, description="Markdown. '## ' headings split it")
    category: str | None = None
    product: str | None = None
    scenario_id: str | None = Field(None, max_length=40, description="Optional answer key used by evals")


class ClassIn(BaseModel):
    kind: Literal["category", "product"]
    name: str = Field(pattern=CLASS_NAME, description="lower_case_with_underscores")
    description: str | None = Field(None, max_length=300)


class ApproveIn(BaseModel):
    name: str = Field(pattern=CLASS_NAME, description="The final name for the new class")
    description: str | None = Field(None, max_length=300)


def doc_type_of(doc_id: str) -> str:
    if doc_id.startswith("KB-"):
        return "kb"
    if doc_id.startswith("T-"):
        return "ticket"
    raise HTTPException(status_code=404, detail="Unknown document ID. IDs start with 'T-' or 'KB-'.")


def add_data_routes(app: FastAPI, any_key, admin_key, log) -> None:
    """Attach the endpoints. `any_key` and `admin_key` are the gateway's authorisation checks."""

    def database(call, *args):
        """Run a database call. If the database is down, say so clearly instead of crashing."""
        try:
            return call(*args)
        except HTTPException:
            raise
        except Exception as error:  # noqa: BLE001
            log.error("database unavailable", extra={"fields": {"error": str(error)}})
            raise HTTPException(status_code=503, detail="The database is unavailable. Try again.") from error

    def check_classes(store, category: str | None, product: str | None) -> None:
        """A document may only use classes that exist. This stops a typo from creating a new class."""
        known = database(store.taxonomy)
        problems = []
        for kind, value in (("category", category), ("product", product)):
            if value is not None and value not in {item["name"] for item in known[kind]}:
                problems.append(f"unknown {kind} '{value}'")
        if problems:
            raise HTTPException(
                status_code=422,
                detail=f"{' and '.join(problems)}. Add the class first with POST /v1/taxonomy, "
                "or approve a proposal.",
            )

    def accepted(request: Request, doc_type: str, doc_id: str, action: str, **extra) -> dict:
        queued = request.app.state.queue.publish(doc_type, doc_id)
        DOCUMENTS.labels(doc_type, action).inc()
        log.info(
            "document accepted",
            extra={"fields": {"doc_type": doc_type, "doc_id": doc_id, "action": action, "queued": queued}},
        )
        # Not queued (Redis down) is not an error: the worker's sweep will pick the document up.
        return {"id": doc_id, "type": doc_type, "status": "accepted", "queued": queued, **extra}

    # ---- classes --------------------------------------------------------------------------------

    @app.get("/v1/taxonomy", tags=["classes"])
    def taxonomy(request: Request, caller: str = Depends(any_key)) -> dict:
        return database(request.app.state.store.taxonomy)

    @app.post("/v1/taxonomy", status_code=201, tags=["classes"])
    def add_class(body: ClassIn, request: Request, caller: str = Depends(admin_key)) -> dict:
        database(request.app.state.store.add_class, body.kind, body.name, body.description)
        CLASSES.labels("added").inc()
        log.info("class added", extra={"fields": {"kind": body.kind, "name": body.name, "caller": caller}})
        return {"kind": body.kind, "name": body.name, "status": "active"}

    @app.delete("/v1/taxonomy/{kind}/{name}", tags=["classes"])
    def retire_class(
        kind: Literal["category", "product"], name: str, request: Request, caller: str = Depends(admin_key)
    ) -> dict:
        if not database(request.app.state.store.retire_class, kind, name):
            raise HTTPException(status_code=404, detail="No active class with that name")
        CLASSES.labels("retired").inc()
        log.info("class retired", extra={"fields": {"kind": kind, "name": name, "caller": caller}})
        return {"kind": kind, "name": name, "status": "retired"}

    # ---- documents ------------------------------------------------------------------------------

    @app.post("/v1/tickets", status_code=202, tags=["documents"])
    def save_ticket(body: TicketIn, request: Request, caller: str = Depends(admin_key)) -> dict:
        store = request.app.state.store
        check_classes(store, body.category, body.product)
        ticket = body.model_dump()
        ticket["id"] = body.id or f"T-{uuid.uuid4().hex[:10].upper()}"
        database(store.save_ticket, ticket)
        return accepted(request, "ticket", ticket["id"], "saved")

    @app.put("/v1/kb/{article_id}", status_code=202, tags=["documents"])
    def save_article(
        article_id: str, body: ArticleIn, request: Request, caller: str = Depends(admin_key)
    ) -> dict:
        if doc_type_of(article_id) != "kb":
            raise HTTPException(status_code=422, detail="Article IDs start with 'KB-'")
        store = request.app.state.store
        check_classes(store, body.category, body.product)
        version = database(store.save_article, {**body.model_dump(), "id": article_id})
        return accepted(request, "kb", article_id, "saved", version=version)

    @app.delete("/v1/documents/{doc_id}", status_code=202, tags=["documents"])
    def retire_document(doc_id: str, request: Request, caller: str = Depends(admin_key)) -> dict:
        doc_type = doc_type_of(doc_id)
        if not database(request.app.state.store.retire_document, doc_type, doc_id):
            raise HTTPException(status_code=404, detail="No such document")
        return accepted(request, doc_type, doc_id, "retired")

    @app.get("/v1/documents/{doc_id}", tags=["documents"])
    def document_status(doc_id: str, request: Request, caller: str = Depends(admin_key)) -> dict:
        status = database(request.app.state.store.document_status, doc_type_of(doc_id), doc_id)
        if status is None:
            raise HTTPException(status_code=404, detail="No such document")
        return status

    @app.get("/v1/ingest/status", tags=["documents"])
    def ingest_status(request: Request, caller: str = Depends(admin_key)) -> dict:
        return {"documents_waiting": database(request.app.state.store.documents_waiting)}

    # ---- proposals for new classes ----------------------------------------------------------------

    @app.get("/v1/classes/proposals", tags=["classes"])
    def proposals(
        request: Request,
        status: Literal["pending", "approved", "rejected"] = "pending",
        caller: str = Depends(admin_key),
    ) -> dict:
        return {"proposals": database(request.app.state.store.proposals, status)}

    @app.post("/v1/classes/proposals/{proposal_id}/approve", tags=["classes"])
    def approve(
        proposal_id: int, body: ApproveIn, request: Request, caller: str = Depends(admin_key)
    ) -> dict:
        decided = database(
            request.app.state.store.decide_proposal, proposal_id, True, body.name, body.description
        )
        if decided is None:
            raise HTTPException(status_code=404, detail="No pending proposal with that ID")
        CLASSES.labels("proposal_approved").inc()
        log.info("proposal approved", extra={"fields": {"proposal": proposal_id, "name": body.name}})
        return decided

    @app.post("/v1/classes/proposals/{proposal_id}/reject", tags=["classes"])
    def reject(proposal_id: int, request: Request, caller: str = Depends(admin_key)) -> dict:
        decided = database(request.app.state.store.decide_proposal, proposal_id, False)
        if decided is None:
            raise HTTPException(status_code=404, detail="No pending proposal with that ID")
        CLASSES.labels("proposal_rejected").inc()
        return decided
