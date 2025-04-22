CREATE TABLE faq (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL
);

INSERT INTO faq (question, answer) VALUES
("什麼是MCP?", "MCP代表Model Context Protocol，用於管理AI模型的上下文。"),
("MCP如何與LangChain整合?", "MCP可用於維護LangChain的對話記憶，並與RAG檢索系統結合。");
