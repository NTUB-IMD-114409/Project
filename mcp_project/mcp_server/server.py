from fastapi import FastAPI
from pydantic import BaseModel
from mcp_server.langchain_pipeline import qa

app = FastAPI()

class MCPRequest(BaseModel):
    cmd: str
    query: str = None

@app.post("/mcp")
async def handle_mcp(request: MCPRequest):
    if request.cmd == "ASK":
        response = qa.invoke({"question": request.query})
        return {"cmd": "RESULT", "response": response["answer"]}

    return {"cmd": "ERROR", "message": "未知的 MCP 指令"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
