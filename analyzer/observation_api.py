"""API de lectura para una corrida de k6 y una referencia opcional de PetClinic."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from latency_analysis import latency_analysis
from reference_model import load_model, score_window


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vets_p95_ms: FiniteFloat = Field(ge=0)
    process_cpu_ratio: FiniteFloat = Field(ge=0, le=1)
    heap_mib: FiniteFloat = Field(ge=0)
    gc_pause_seconds: FiniteFloat = Field(ge=0)
    pool_active_mean: FiniteFloat = Field(ge=0)


def create_app(k6_path=None, deployments_path=None, model_path=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.reference = load_model(Path(model_path)) if model_path else None
        yield

    app = FastAPI(title="PetClinic AIOps", lifespan=lifespan)

    @app.get("/observation/latency")
    def latency(min_samples: int = Query(2, ge=2), block_seconds: int | None = Query(None, ge=1)):
        if not k6_path:
            raise HTTPException(503, "No se ha configurado una corrida de k6")
        try:
            return latency_analysis(Path(k6_path), Path(deployments_path) if deployments_path else None,
                                    min_samples, block_seconds)
        except (OSError, EOFError, ValueError, KeyError) as exc:
            raise HTTPException(503, "Evidencia k6 incompleta, ilegible o incompatible") from exc

    @app.post("/observation/deviation")
    def deviation(window: Window):
        if app.state.reference is None:
            raise HTTPException(503, "Referencia de Isolation Forest pendiente")
        return score_window(app.state.reference, window.model_dump())

    return app


app = create_app(os.getenv("PETCLINIC_K6_FILE"), os.getenv("PETCLINIC_DEPLOYMENTS"),
                 os.getenv("PETCLINIC_MODEL"))
