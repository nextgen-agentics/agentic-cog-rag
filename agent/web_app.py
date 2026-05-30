import asyncio
import os
import sys
import logging
from pathlib import Path
from typing import Generator
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web_app")

# Resolve paths
AGENT_DIR = Path(__file__).parent.resolve()
SANDBOX = AGENT_DIR / "sandbox"
SANDBOX.mkdir(exist_ok=True)

# Load env variables (ensures Tavily/LLM keys are present)
load_dotenv(AGENT_DIR / ".env")

app = FastAPI(title="Agent Control Hub API")

# Global dict to track active running processes
active_processes = {}

# Define payload models
class ConvertRequest(BaseModel):
    input_dir: str
    output_dir: str | None = None
    overwrite: bool = False

class IndexRequest(BaseModel):
    input_dir: str

class AgentRequest(BaseModel):
    query: str

class StopRequest(BaseModel):
    task_type: str

# Helper to format paths and ensure they remain inside sandbox if specified
def resolve_sandbox_path(path_str: str) -> Path:
    """Resolve input paths. Supports paths relative to the agent directory or absolute paths."""
    p = Path(path_str)
    if p.is_absolute():
        return p.resolve()
    return (AGENT_DIR / p).resolve()

async def run_command_stream(process, task_type: str) -> Generator[str, None, None]:
    """Streams unified stdout/stderr of a given process and manages its registration in active_processes."""
    active_processes[task_type] = process
    logger.info(f"Registered active process for {task_type} (PID: {process.pid})")
    
    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            # Yield lines back to the SSE / chunked streaming client
            yield line.decode("utf-8", errors="replace")
    except asyncio.CancelledError:
        logger.warning(f"Process stream for {task_type} cancelled by client. Terminating process...")
        try:
            process.terminate()
            await process.wait()
        except Exception as e:
            logger.error(f"Error terminating subprocess: {e}")
    finally:
        await process.wait()
        if task_type in active_processes and active_processes[task_type] == process:
            del active_processes[task_type]
        logger.info(f"Subprocess for {task_type} finished with exit code {process.returncode}")

@app.get("/api/status")
async def get_status():
    """Checks if the llm_gatewayV7 is up and running on port 8107."""
    gateway_url = "http://localhost:8107/v1/routers"
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            resp = await client.get(gateway_url)
            if resp.status_code == 200:
                return {"gateway": "online", "url": "http://localhost:8107"}
    except Exception:
        pass
    return {"gateway": "offline", "url": "http://localhost:8107"}

@app.post("/api/stop")
async def stop_process(req: StopRequest):
    """Terminates an active process by task type."""
    logger.info(f"Stop request received for task type: {req.task_type}")
    process = active_processes.get(req.task_type)
    if process:
        try:
            logger.info(f"Terminating active subprocess {process.pid} for '{req.task_type}'")
            process.terminate()
            await process.wait()
            if req.task_type in active_processes:
                del active_processes[req.task_type]
            return {"status": "stopped", "message": f"Successfully stopped active process for '{req.task_type}'"}
        except Exception as e:
            logger.error(f"Error terminating process {process.pid}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to terminate process: {e}")
    return {"status": "inactive", "message": f"No active process found for '{req.task_type}'"}

@app.post("/api/convert")
async def convert_pdfs(req: ConvertRequest):
    """Triggers convert_pdfs_to_markdown.py and streams output."""
    in_path = resolve_sandbox_path(req.input_dir)
    if not in_path.exists():
        raise HTTPException(status_code=400, detail=f"Input directory does not exist: {req.input_dir}")
        
    cmd = [sys.executable, "-u", "convert_pdfs_to_markdown.py", "--input-dir", str(in_path)]
    
    if req.output_dir:
        out_path = resolve_sandbox_path(req.output_dir)
        cmd += ["--output-dir", str(out_path)]
        
    if req.overwrite:
        cmd += ["--overwrite"]
        
    # cmd += ["--verbose"]
    
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(AGENT_DIR),
        env=env
    )
    
    return StreamingResponse(run_command_stream(process, "convert"), media_type="text/plain")

@app.post("/api/index")
async def index_markdown(req: IndexRequest):
    """Triggers index_markdown_docs.py and streams output."""
    in_path = resolve_sandbox_path(req.input_dir)
    if not in_path.exists():
        raise HTTPException(status_code=400, detail=f"Input directory does not exist: {req.input_dir}")
        
    # index_markdown_docs.py takes the folder as a positional argument
    cmd = [sys.executable, "-u", "index_markdown_docs.py", str(in_path)]
    
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(AGENT_DIR),
        env=env
    )
    
    return StreamingResponse(run_command_stream(process, "index"), media_type="text/plain")

@app.post("/api/agent")
async def run_agent(req: AgentRequest):
    """Triggers agent7.py and streams output."""
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
        
    cmd = [sys.executable, "-u", "agent7.py", req.query]
    
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(AGENT_DIR),
        env=env
    )
    
    return StreamingResponse(run_command_stream(process, "agent"), media_type="text/plain")

# Serve the single-page application static files
web_folder = AGENT_DIR / "web"
web_folder.mkdir(exist_ok=True)

# Mount frontend asset subfolders if they exist (we serve index.html directly)
app.mount("/static", StaticFiles(directory=str(web_folder)), name="static")

@app.get("/")
async def get_index():
    index_file = web_folder / "index.html"
    if not index_file.exists():
        # Fallback inline response while assets are being written
        return {"message": "Server online. Frontend assets missing in code/agent/web/"}
    return FileResponse(str(index_file))
