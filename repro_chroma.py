import chromadb
client = chromadb.Client()
collection = client.create_collection("test")
collection.upsert(
    ids=["1"],
    documents=["test"],
    metadatas=[{"decorators": []}]
)
print("Done")
