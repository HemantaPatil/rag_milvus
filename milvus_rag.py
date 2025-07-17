#!/usr/bin/env python3

import json
import os
import sys
import argparse
from typing import List, Dict, Any

from pymilvus import (
    connections,
    utility,
    Collection,
)
from langchain.chains import ConversationalRetrievalChain
from langchain.memory import ConversationBufferMemory
from langchain.vectorstores import Milvus
from langchain.document_loaders import PyPDFLoader, PyPDFDirectoryLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_openai import OpenAI
from langchain_huggingface import HuggingFaceEmbeddings
from dotenv import load_dotenv
load_dotenv()

class MilvusRAGManager:
    def __init__(self, config_path: str):
        self.config = self._load_config(config_path)
        self.embeddings = None
        self.llm = None
        self.vector_db = None
        self.collection = None
        self.text_splitter = None
        self._setup_components()

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Config file not found: {config_path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in config file: {e}")

    def _setup_components(self):
        """Initialize all components based on configuration"""
        # Setup embeddings
        model_name = self.config.get('embedding_model', 'all-mpnet-base-v2')
        self.embeddings = HuggingFaceEmbeddings(model_name=model_name)
        
        # Setup LLM
        self.llm = OpenAI()
        
        # Setup text splitter
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.config.get('chunk_size', 500),
            chunk_overlap=self.config.get('chunk_overlap', 20),
            length_function=len,
            add_start_index=True,
        )
        
        # Connect to Milvus
        self._connect_milvus()

    def _connect_milvus(self):
        """Connect to Milvus database"""
        host = self.config['docker_host']
        port = self.config['docker_port']
        
        connections.connect(host=host, port=port)
        print(f"Connected to Milvus at {host}:{port}")
        print(f"Server version: {utility.get_server_version()}")

    def _create_collection_if_not_exists(self):
        """Create collection if it doesn't exist"""
        collection_name = self.config['collection_name']
        
        if not utility.has_collection(collection_name):
            print(f"Collection '{collection_name}' does not exist. Will be created during first insert.")
        else:
            print(f"Collection '{collection_name}' already exists.")
            self.collection = Collection(collection_name)

    def _get_connection_args(self) -> Dict[str, str]:
        """Get connection arguments for Milvus"""
        return {
            "host": self.config['docker_host'],
            "port": str(self.config['docker_port'])
        }

    def _get_search_params(self) -> Dict[str, Any]:
        """Get search parameters"""
        return {
            "metric": "IP",
            "offset": 0,
            "limit": 10,
        }

    def _get_index_params(self) -> Dict[str, Any]:
        """Get index parameters"""
        return {
            "index_type": "IVF_FLAT",
            "metric_type": "L2",
            "params": {"nlist": 128},
        }

    def _load_documents(self, file_path: str) -> List:
        """Load documents from file or directory"""
        file_type = self.config.get('file_type', 'pdf').lower()
        
        if os.path.isfile(file_path):
            if file_type == 'pdf':
                loader = PyPDFLoader(file_path)
                documents = loader.load()
                return self.text_splitter.split_documents(documents)
            else:
                raise ValueError(f"Unsupported file type: {file_type}")
        
        elif os.path.isdir(file_path):
            if file_type == 'pdf':
                loader = PyPDFDirectoryLoader(file_path)
                documents = loader.load()
                return self.text_splitter.split_documents(documents)
            else:
                raise ValueError(f"Unsupported file type for directory: {file_type}")
        
        else:
            raise FileNotFoundError(f"File or directory not found: {file_path}")

    def insert_documents(self, file_path: str):
        """Insert documents into Milvus collection"""
        print(f"Inserting documents from: {file_path}")
        
        documents = self._load_documents(file_path)
        
        self.vector_db = Milvus.from_documents(
            documents=documents,
            embedding=self.embeddings,
            connection_args=self._get_connection_args(),
            collection_name=self.config['collection_name'],
            search_params=self._get_search_params(),
            index_params=self._get_index_params(),
        )
        
        print(f"Successfully inserted {len(documents)} document chunks")

    def delete_documents(self, file_path: str):
        """Delete documents from Milvus collection"""
        if not self.collection:
            self.collection = Collection(self.config['collection_name'])
        
        # Query for documents from the specific file
        res = self.collection.query(
            expr=f"source == '{file_path}'",
            output_fields=["pk", "source"]
        )
        
        if not res:
            print(f"No documents found for file: {file_path}")
            return
        
        # Delete each document
        for doc in res:
            expr = f"pk in [{doc['pk']}]"
            self.collection.delete(expr)
            print(f"Deleted document with pk: {doc['pk']}")
        
        print(f"Successfully deleted {len(res)} documents from {file_path}")

    def update_documents(self, file_path: str):
        """Update documents (delete and re-insert)"""
        print(f"Updating documents from: {file_path}")
        
        # Check if documents exist
        if self._check_file_exists(file_path):
            self.delete_documents(file_path)
        
        # Insert new documents
        self.insert_documents(file_path)

    def _check_file_exists(self, file_path: str) -> bool:
        """Check if file exists in collection"""
        if not self.collection:
            self.collection = Collection(self.config['collection_name'])
        
        res = self.collection.query(
            expr=f"source == '{file_path}'",
            output_fields=["pk", "source"]
        )
        
        return len(res) > 0

    def query_documents(self, query: str, use_rag: bool = True):
        """Query documents using similarity search or RAG"""
        if not self.vector_db:
            self.vector_db = Milvus(
                self.embeddings,
                connection_args=self._get_connection_args(),
                collection_name=self.config['collection_name'],
            )
        
        if use_rag:
            # Use RAG for more comprehensive answers
            memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)
            pdf_qa = ConversationalRetrievalChain.from_llm(
                self.llm,
                self.vector_db.as_retriever(),
                memory=memory
            )
            
            result = pdf_qa({"question": query})
            return result["answer"]
        else:
            # Simple similarity search
            docs = self.vector_db.similarity_search(query)
            return docs

    def list_collections(self):
        """List all collections in Milvus"""
        collections = utility.list_collections()
        print("Available collections:")
        for collection in collections:
            print(f"  - {collection}")
        return collections

    def get_collection_stats(self):
        """Get statistics about the current collection"""
        if not self.collection:
            self.collection = Collection(self.config['collection_name'])
        
        stats = self.collection.get_stats()
        print(f"Collection '{self.config['collection_name']}' statistics:")
        print(f"  Row count: {stats.row_count}")
        return stats

    def _drop_collection(self):
        """Drop the current collection"""
        collection_name = self.config['collection_name']
        
        if utility.has_collection(collection_name):
            utility.drop_collection(collection_name)
            print(f"Collection '{collection_name}' dropped successfully")
            self.collection = None
            self.vector_db = None
        else:
            print(f"Collection '{collection_name}' does not exist")

    def load_collection(self, file_path: str):
        """Load documents into a fresh collection (drops existing if present)"""
        # Drop collection if it exists to avoid schema conflicts
        self._drop_collection()
        
        print(f"Loading documents from: {file_path}")
        documents = self._load_documents(file_path)
        
        self.vector_db = Milvus.from_documents(
            documents=documents,
            embedding=self.embeddings,
            connection_args=self._get_connection_args(),
            collection_name=self.config['collection_name'],
            search_params=self._get_search_params(),
            index_params=self._get_index_params(),
        )
        
        print(f"Successfully loaded {len(documents)} document chunks into new collection")
        self.collection = Collection(self.config['collection_name'])


def main():
    parser = argparse.ArgumentParser(description='Milvus RAG Document Manager')
    parser.add_argument('--config', required=True, help='Path to JSON config file')
    parser.add_argument('--operation', required=True, choices=['insert', 'update', 'delete', 'query', 'drop', 'load'], 
                       help='CRUD operation to perform')
    parser.add_argument('--file-path', help='Path to file or directory to process')
    parser.add_argument('--query', help='Query string for search operations')
    parser.add_argument('--use-rag', action='store_true', help='Use RAG for query (default: similarity search)')
    
    args = parser.parse_args()
    
    try:
        manager = MilvusRAGManager(args.config)
        
        if args.operation == 'insert':
            if not args.file_path:
                print("Error: --file-path is required for insert operation")
                sys.exit(1)
            manager.insert_documents(args.file_path)
        
        elif args.operation == 'update':
            if not args.file_path:
                print("Error: --file-path is required for update operation")
                sys.exit(1)
            manager.update_documents(args.file_path)
        
        elif args.operation == 'delete':
            if not args.file_path:
                print("Error: --file-path is required for delete operation")
                sys.exit(1)
            manager.delete_documents(args.file_path)
        
        elif args.operation == 'query':
            if not args.query:
                print("Error: --query is required for query operation")
                sys.exit(1)
            result = manager.query_documents(args.query, args.use_rag)
            print("Query result:")
            print(result)
        
        elif args.operation == 'drop':
            manager._drop_collection()
        
        elif args.operation == 'load':
            if not args.file_path:
                print("Error: --file-path is required for load operation")
                sys.exit(1)
            manager.load_collection(args.file_path)
        
        # Show collection stats (skip if collection was dropped)
        if args.operation != 'drop':
            manager.get_collection_stats()
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()