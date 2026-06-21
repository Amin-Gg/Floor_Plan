"""rag — retrieval and regulation-knowledge-graph layer.

Modules
-------
embeddings, rag_index, rag_retriever      : dense/hybrid retrieval over pgvector
crag_retriever, query_router, query_transforms, retrieval_evaluator : adaptive (CRAG) layer
reranker, contextualize                   : cross-encoder rerank + contextual retrieval
build_regulation_graph, graph_retriever   : NetworkX regulation graph + graph-aware retrieval
groq_client                               : LLM backend used by the query transforms
schema.sql                                : pgvector table + HNSW index DDL
"""
