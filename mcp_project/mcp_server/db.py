from sqlalchemy import create_engine
from langchain_community.utilities import SQLDatabase

from mcp_server.config import DB_URI

engine = create_engine(DB_URI)
database = SQLDatabase(engine)
