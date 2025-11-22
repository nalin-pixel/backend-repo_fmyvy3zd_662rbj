import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import db, create_document, get_documents

app = FastAPI(title="RISE API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# Helpers
# -----------------------------

def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(doc)
    if "_id" in d:
        d["id"] = str(d.pop("_id"))
    # Convert datetimes to isoformat
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.astimezone(timezone.utc).isoformat()
    return d


# -----------------------------
# Schemas (lightweight for API IO)
# -----------------------------

class OnboardingInput(BaseModel):
    goals: List[str] = Field(..., min_items=1, max_items=5)
    blocker: str = Field(..., description="Main constraint or blocker")
    work_hours: Optional[str] = Field("9-6", description="Typical work hours, e.g., '9-6'")
    energy_pattern: Optional[str] = Field("low-evening", description="Energy hint: low-evening, morning-person, etc.")

class ProposedBlock(BaseModel):
    title: str
    start: str
    end: str
    category: str

class OnboardingPlan(BaseModel):
    protocol_name: str
    blocks: List[ProposedBlock]
    message: str

class AcceptPlanInput(BaseModel):
    accept: bool

class TaskCreate(BaseModel):
    title: str
    start: str
    end: str
    category: str
    status: str = "scheduled"

class TaskOut(BaseModel):
    id: str
    title: str
    start: str
    end: str
    category: str
    status: str


# -----------------------------
# Routes
# -----------------------------

@app.get("/")
def read_root():
    return {"message": "RISE API running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = os.getenv("DATABASE_NAME") or "❌ Not Set"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
                response["connection_status"] = "Connected"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:80]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response


@app.post("/api/onboarding/propose", response_model=OnboardingPlan)
def propose_onboarding(data: OnboardingInput):
    # Simple heuristic to place blocks based on energy pattern
    today = datetime.now().date()

    def iso(day_offset: int, hour: int, minute: int = 0, duration_min: int = 30):
        start_dt = datetime.combine(today + timedelta(days=day_offset), datetime.min.time()).replace(hour=hour, minute=minute)
        end_dt = start_dt + timedelta(minutes=duration_min)
        return start_dt.isoformat(), end_dt.isoformat()

    blocks: List[ProposedBlock] = []

    # Morning micro-learning if evenings are low energy
    if "low" in (data.energy_pattern or ""):  # low-evening
        s, e = iso(0, 7, 0, 25)
        blocks.append(ProposedBlock(title="Python Micro‑Learning", start=s, end=e, category="mind"))
    else:
        s, e = iso(0, 19, 0, 25)
        blocks.append(ProposedBlock(title="Python Micro‑Learning", start=s, end=e, category="mind"))

    # Midday Zone 2 run
    s, e = iso(0, 12, 30, 40)
    blocks.append(ProposedBlock(title="Zone 2 Run", start=s, end=e, category="fitness"))

    # Evening meal prep
    s, e = iso(0, 18, 0, 45)
    blocks.append(ProposedBlock(title="Meal Prep", start=s, end=e, category="vitality"))

    message = (
        "Here is your new Base Protocol. Accept to generate your schedule and start earning XP."
    )

    return OnboardingPlan(protocol_name="Base Protocol", blocks=blocks, message=message)


@app.post("/api/onboarding/accept")
def accept_onboarding(plan: List[TaskCreate]):
    # Create a profile if not exists
    profile = db["profile"].find_one({}) if db else None
    if profile is None:
        profile_id = create_document("profile", {"level": 1, "xp": 0, "streak": 0})
    else:
        profile_id = str(profile.get("_id"))

    # Insert tasks
    inserted: List[str] = []
    for t in plan:
        doc = t.model_dump()
        inserted_id = create_document("task", doc)
        inserted.append(inserted_id)

    return {"ok": True, "profile_id": profile_id, "created": len(inserted)}


@app.get("/api/tasks", response_model=List[TaskOut])
def list_tasks(status: Optional[str] = None):
    filter_q: Dict[str, Any] = {}
    if status:
        filter_q["status"] = status
    rows = get_documents("task", filter_q)
    # sort by start
    rows.sort(key=lambda r: r.get("start", ""))
    out: List[TaskOut] = []
    for r in rows:
        sr = serialize_doc(r)
        out.append(TaskOut(**{
            "id": sr["id"],
            "title": sr.get("title", ""),
            "start": sr.get("start", ""),
            "end": sr.get("end", ""),
            "category": sr.get("category", "misc"),
            "status": sr.get("status", "scheduled"),
        }))
    return out


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")

    from bson import ObjectId

    task = db["task"].find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    db["task"].update_one({"_id": ObjectId(task_id)}, {"$set": {"status": "done", "updated_at": datetime.now(timezone.utc)}})

    # Award simple XP by category
    xp_map = {"fitness": 20, "mind": 15, "vitality": 10, "wealth": 15, "charisma": 10}
    xp_gain = xp_map.get(task.get("category", ""), 10)

    profile = db["profile"].find_one({})
    if profile:
        new_xp = int(profile.get("xp", 0)) + xp_gain
        new_level = int(profile.get("level", 1))
        # Level up every 100 xp
        while new_xp >= 100:
            new_level += 1
            new_xp -= 100
        db["profile"].update_one({"_id": profile["_id"]}, {"$set": {"xp": new_xp, "level": new_level, "updated_at": datetime.now(timezone.utc)}})

    return {"ok": True, "xp_gain": xp_gain}


@app.get("/api/profile")
def get_profile():
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    profile = db["profile"].find_one({})
    if not profile:
        return {"level": 1, "xp": 0, "streak": 0}
    p = serialize_doc(profile)
    return {"level": p.get("level", 1), "xp": p.get("xp", 0), "streak": p.get("streak", 0)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
