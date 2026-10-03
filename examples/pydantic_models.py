"""Optional constraints and aliases: ``uv run --extra pydantic mic run examples.pydantic_models:double``."""

from pydantic import BaseModel, ConfigDict, Field

import mic
from mic.integrations.pydantic import pydantic_schema


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=0, alias="quantity")


class Answer(BaseModel):
    count: int = Field(ge=0)


@mic.dataset(
    name="pydantic.requests",
    schema=mic.case_schema(input=pydantic_schema(Request), expected=pydantic_schema(Answer)),
)
def requests() -> list[mic.RawCase]:
    return [mic.RawCase(id="two", input={"quantity": 2}, expected={"count": 4})]


@mic.scorer(name="exact")
def exact(ctx: mic.ScoreContext[Request, Answer, Answer, mic.JsonObject]) -> float:
    return float(ctx.output == ctx.require_expected())


@mic.eval(name="pydantic.double", dataset=requests, output=pydantic_schema(Answer), scorers=[exact])
def double(ctx: mic.TaskContext[mic.JsonObject], request: Request) -> Answer:
    return Answer(count=request.count * 2)
