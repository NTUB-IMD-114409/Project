import requests

def send_mcp_request(cmd, query=""):
    url = "http://127.0.0.1:8000/mcp"
    data = {"cmd": cmd, "query": query}
    response = requests.post(url, json=data)
    print("🔹 伺服器回應:", response.json())

if __name__ == "__main__":
    send_mcp_request("ASK", "什麼是MCP?")
    send_mcp_request("ASK", "MCP如何與LangChain整合?")
