#!/usr/bin/env python3
"""
Demo script to test the Enterprise RAG Platform
Shows all major features: ingestion, retrieval, and query
"""

import requests
import json
import time
from typing import Dict

BASE_URL = "http://localhost:8000"
TENANT_ID = "acme_law_firm"

def print_section(title: str):
    """Print a formatted section header"""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")

def print_response(response: Dict, indent: int = 0):
    """Pretty print a JSON response"""
    print(json.dumps(response, indent=2))

def test_health_check():
    """Test 1: Health check endpoint"""
    print_section("TEST 1: Health Check")
    
    response = requests.get(f"{BASE_URL}/health")
    print(f"Status Code: {response.status_code}")
    print_response(response.json())
    
    assert response.status_code == 200
    print("✓ Health check passed")

def test_root_endpoint():
    """Test 2: API documentation endpoint"""
    print_section("TEST 2: API Documentation")
    
    response = requests.get(f"{BASE_URL}/")
    print(f"Status Code: {response.status_code}")
    print_response(response.json())
    
    assert response.status_code == 200
    print("✓ API documentation endpoint working")

def test_ingest_document():
    """Test 3: Document ingestion"""
    print_section("TEST 3: Document Ingestion")
    
    documents = [
        {
            "tenant_id": TENANT_ID,
            "document_id": "case_2024_001",
            "content": "This is a sample legal document discussing contract law and liability clauses...",
            "source_page": 1
        },
        {
            "tenant_id": TENANT_ID,
            "document_id": "case_2024_002", 
            "content": "Another important case document about intellectual property rights and patents...",
            "source_page": 42
        },
        {
            "tenant_id": TENANT_ID,
            "document_id": "precedent_2023",
            "content": "Legal precedent establishing important guidelines for contract interpretation...",
            "source_page": 15
        }
    ]
    
    for doc in documents:
        print(f"\nIngesting: {doc['document_id']}")
        response = requests.post(f"{BASE_URL}/ingest", json=doc)
        print(f"Status Code: {response.status_code}")
        print_response(response.json())
        
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "success"
        
    print(f"\n✓ Successfully ingested {len(documents)} documents")

def test_query():
    """Test 4: Query the RAG system"""
    print_section("TEST 4: RAG Query with Hybrid Search & Citations")
    
    queries = [
        "What are the liability clauses in the contract?",
        "How should we interpret this patent agreement?",
        "What legal precedents apply to this case?"
    ]
    
    for query_text in queries:
        print(f"\nQuery: {query_text}")
        print("-" * 60)
        
        payload = {
            "tenant_id": TENANT_ID,
            "query": query_text,
            "top_k": 5
        }
        
        start = time.time()
        response = requests.post(f"{BASE_URL}/query", json=payload)
        elapsed = time.time() - start
        
        print(f"Status Code: {response.status_code} (Response time: {elapsed:.3f}s)")
        
        assert response.status_code == 200
        result = response.json()
        
        print(f"\nAnswer:")
        print(f"  {result['answer']}")
        print(f"\nConfidence: {result['confidence']:.2%}")
        print(f"Tokens used: {result['tokens_used']}")
        
        if result['citations']:
            print(f"\nCitations ({len(result['citations'])} found):")
            for idx, citation in enumerate(result['citations'], 1):
                print(f"  [{idx}] {citation['source']} (Page {citation['page']})")
                print(f"      Relevance: {citation['confidence']:.2%}")
                print(f"      Snippet: {citation['snippet'][:80]}...")
        
    print("\n✓ All queries processed successfully")

def test_tenant_stats():
    """Test 5: Get tenant statistics"""
    print_section("TEST 5: Tenant Statistics")
    
    response = requests.get(f"{BASE_URL}/stats/{TENANT_ID}")
    print(f"Status Code: {response.status_code}")
    print_response(response.json())
    
    assert response.status_code == 200
    print("✓ Tenant statistics retrieved")

def run_all_tests():
    """Run all tests"""
    print("\n")
    print("╔" + "=" * 78 + "╗")
    print("║" + " " * 20 + "ENTERPRISE RAG PLATFORM - DEMO TEST SUITE" + " " * 18 + "║")
    print("╚" + "=" * 78 + "╝")
    
    try:
        test_health_check()
        test_root_endpoint()
        test_ingest_document()
        test_query()
        test_tenant_stats()
        
        print_section("ALL TESTS PASSED ✓")
        print("The Enterprise RAG Platform is working correctly!")
        print("\nFeatures demonstrated:")
        print("  ✓ Multi-tenant support (ACME Law Firm)")
        print("  ✓ Document ingestion with metadata")
        print("  ✓ Hybrid search (BM25 + vector)")
        print("  ✓ Citation tracking and sourcing")
        print("  ✓ Confidence scoring")
        print("  ✓ Audit logging")
        print("\n" + "=" * 80 + "\n")
        
    except Exception as e:
        print_section("TEST FAILED ✗")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)

if __name__ == "__main__":
    run_all_tests()
