#!/usr/bin/env python3
"""
Test script for the new JSON endpoints of the Campaign Plan Generator API.
"""

import requests
import json
from datetime import date

# Test data
test_campaign_data = {
    "candidate_name": "Jane Smith",
    "primary_date": None,
    "election_date": "2025-11-05",
    "office_and_jurisdiction": "City Council, District 3, Boston, MA",
    "incumbent_status": "N/A",
    "race_type": "Nonpartisan",
    "seats_available": 1,
    "number_of_opponents": 3,
    "win_number": 8000,
    "total_likely_voters": 50000,
    "available_cell_phones": 5000,
    "available_landlines": 500,
    "additional_race_context": "Focus on neighborhood safety and small business support"
}

def test_generate_campaign_plan_json():
    """Test the /generate-campaign-plan endpoint with format=json"""
    print("Testing /generate-campaign-plan?format=json...")
    
    try:
        response = requests.post(
            "http://localhost:8000/generate-campaign-plan?format=json",
            json=test_campaign_data,
            timeout=120
        )
        
        if response.status_code == 200:
            json_data = response.json()
            print("✅ JSON endpoint successful!")
            print(f"Response keys: {list(json_data.keys())}")
            
            if "tasks" in json_data:
                timeline_tasks = json_data["tasks"]["timeline"]
                voter_contact_tasks = json_data["tasks"]["voter_contact"]
                print(f"Timeline tasks found: {len(timeline_tasks)}")
                print(f"Voter contact tasks found: {len(voter_contact_tasks)}")
                
                if timeline_tasks:
                    print("Sample timeline task:", timeline_tasks[0])
                if voter_contact_tasks:
                    print("Sample voter contact task:", voter_contact_tasks[0])
            
            return True
        else:
            print(f"❌ Error: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False

def test_generate_campaign_plan_pdf():
    """Test the /generate-campaign-plan endpoint with format=pdf (default)"""
    print("\nTesting /generate-campaign-plan (PDF format)...")
    
    try:
        response = requests.post(
            "http://localhost:8000/generate-campaign-plan",
            json=test_campaign_data,
            timeout=120
        )
        
        if response.status_code == 200:
            content_type = response.headers.get('content-type', '')
            if 'application/pdf' in content_type:
                print("✅ PDF endpoint successful!")
                print(f"PDF size: {len(response.content)} bytes")
                return True
            else:
                print(f"❌ Unexpected content type: {content_type}")
                return False
        else:
            print(f"❌ Error: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False

def test_generate_campaign_plan_json_only():
    """Test the dedicated /generate-campaign-plan-json endpoint"""
    print("\nTesting /generate-campaign-plan-json...")
    
    try:
        response = requests.post(
            "http://localhost:8000/generate-campaign-plan-json",
            json=test_campaign_data,
            timeout=120
        )
        
        if response.status_code == 200:
            json_data = response.json()
            print("✅ JSON-only endpoint successful!")
            print(f"Response keys: {list(json_data.keys())}")
            return True
        else:
            print(f"❌ Error: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False

def main():
    """Run all tests"""
    print("🧪 Testing Campaign Plan JSON Endpoints")
    print("=" * 50)
    
    # Check if server is running
    try:
        response = requests.get("http://localhost:8000/health", timeout=5)
        if response.status_code != 200:
            print("❌ Server health check failed")
            return
    except:
        print("❌ Server not responding. Make sure it's running on port 8000")
        return
    
    print("✅ Server is running")
    
    # Run tests
    tests = [
        test_generate_campaign_plan_json,
        test_generate_campaign_plan_pdf,
        test_generate_campaign_plan_json_only
    ]
    
    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"❌ Test failed with exception: {e}")
            results.append(False)
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 Test Summary:")
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total}")
    
    if passed == total:
        print("🎉 All tests passed!")
    else:
        print("⚠️  Some tests failed. Check the output above.")

if __name__ == "__main__":
    main()
