# Cash Validation Implementation Summary

## Overview
Implemented comprehensive cash validation to ensure cash balance never becomes negative and users cannot proceed with insufficient funds.

## Backend Changes

### 1. Quarter Locking Validation (`backend/app/routes/quarter.py`)
**Change**: Pre-flight validation before locking a quarter
- **What**: Calculates projected closing cash before allowing quarter lock
- **Validation**: Blocks lock if `closing_cash <= 0`
- **Error**: Returns 422 with message showing opening cash and projected closing cash
- **Impact**: Prevents quarter from being locked if it would result in negative cash

### 2. Quarter Creation Validation (`backend/app/services/company_service.py`)
**Change**: Validates opening cash before creating next quarter
- **What**: Checks if prior quarter ended with positive cash
- **Validation**: Blocks quarter creation if `opening_cash <= 0`
- **Error**: Returns ValueError with detailed message
- **Impact**: Prevents progression to next quarter when previous quarter ended with insufficient funds

### 3. Allocation Submission Validation (`backend/app/routes/allocations.py`)
**Change**: Dual-layer validation on allocation submissions
- **Layer 1 - Hard Cash Limit**: 
  - Validates: `available_cash - total_discretionary - fixed_costs >= 0`
  - Ensures cash never goes negative even ignoring buffer
  - Shows exact shortfall amount in error message
- **Layer 2 - Buffer Constraint** (existing, retained):
  - Validates: `total_spend <= (available_cash - fixed_costs - buffer) / 100000`
  - Prevents spending into working capital buffer
- **Impact**: Dual protection - absolute minimum (no negative) + recommended minimum (buffer)

### 4. Helper Functions Made Public (`backend/app/services/quarter_run_service.py`)
**Change**: Exported `load_opening_state()` and `to_allocations()` functions
- **Reason**: Allow quarter locking route to pre-calculate closing cash
- **Impact**: Code reuse without duplication

## Frontend Changes

### 5. Allocation Form Validation (`frontend/components/run/AllocationForm.tsx`)
**Change**: Client-side validation before API submission
- **Validation 1**: Blocks submission if `deptBudgetLakhs <= 0`
- **Validation 2**: Blocks submission if `submissionTotal > deptBudgetLakhs`
- **Error Messages**: Clear, user-friendly messages with exact amounts
- **Impact**: Immediate feedback, prevents unnecessary API calls

### 6. Lock Screen Warning (`frontend/components/run/screens/LockScreen.tsx`)
**Change**: Visual warning system on lock confirmation screen
- **Warning Level 1**: Shows when allocations >= 100% of cash
- **Warning Level 2**: Shows when allocations > 95% of cash
- **Display**: Amber warning box with detailed explanation
- **Impact**: Gives users final chance to review and adjust before locking

## Validation Flow

### Happy Path
1. User enters allocations → Frontend validates available budget
2. User submits allocation → Backend validates against cash - fixed costs - buffer
3. User locks quarter → Backend pre-calculates closing cash
4. If closing cash > 0 → Quarter locks successfully
5. User opens next quarter → Backend validates opening cash > 0

### Error Scenarios Handled

#### Scenario A: User tries to allocate more than available
- **Where caught**: Frontend (AllocationForm.persist) + Backend (allocations._upsert)
- **Message**: "Insufficient cash. Total allocation exceeds available budget."
- **Recovery**: Reduce allocations

#### Scenario B: User tries to lock with insufficient allocations
- **Where caught**: Backend (quarter.lock_quarter)
- **Message**: "Cannot lock quarter: closing cash would be ₹X. Reduce allocations."
- **Recovery**: Go back to allocations and reduce spending

#### Scenario C: User tries to open next quarter with zero cash
- **Where caught**: Backend (company_service.create_quarter)
- **Message**: "Cannot open quarter N: insufficient cash balance (₹0)."
- **Recovery**: Cannot proceed - game over scenario

## Key Rules Enforced

1. ✅ **Cash must NEVER become negative**
2. ✅ **Users cannot allocate more than available cash**
3. ✅ **Users cannot proceed to next quarter when cash is ₹0**
4. ✅ **Quarter locking validates closing cash before execution**
5. ✅ **Allocation validation includes fixed costs in calculation**
6. ✅ **Frontend provides immediate feedback before server validation**
7. ✅ **Clear error messages with exact amounts at every step**

## Files Modified

### Backend
- `backend/app/routes/quarter.py` - Quarter locking validation
- `backend/app/services/company_service.py` - Quarter creation validation
- `backend/app/routes/allocations.py` - Allocation submission validation
- `backend/app/services/quarter_run_service.py` - Helper function exports

### Frontend
- `frontend/components/run/AllocationForm.tsx` - Allocation form validation
- `frontend/components/run/screens/LockScreen.tsx` - Lock confirmation warning

## Testing Recommendations

### Manual Test Cases

1. **Test: Exhaust Budget**
   - Allocate all available cash
   - Try to allocate more → Should show error
   
2. **Test: Lock with Negative Projection**
   - Allocate close to 100% of cash
   - Try to lock → Should show error or warning
   
3. **Test: Zero Cash Progression**
   - Complete quarter with minimal revenue
   - Result in near-zero closing cash
   - Try to open next quarter → Should block
   
4. **Test: Fixed Costs Inclusion**
   - Verify fixed costs are included in validation
   - Allocation + fixed costs should not exceed cash

5. **Test: Warning Display**
   - Allocate 95%+ of cash
   - Go to lock screen → Should see warning
   
6. **Test: Department Budget Zero**
   - Have other departments consume all cash
   - Try to allocate in remaining department → Should block

## Validation Logic Summary

```
Available Cash = Quarter.cash_balance
Fixed Costs = from seed or prior quarter snapshot
Buffer = seed.working_capital_buffer_inr
Total Allocation = sum of all 27 allocation fields (in lakhs)

Validation Rules:
1. Total Allocation (in INR) + Fixed Costs <= Available Cash (HARD LIMIT)
2. Total Allocation (in lakhs) <= (Available Cash - Fixed Costs - Buffer) / 100000 (BUFFER CONSTRAINT)
3. Closing Cash = Opening Cash + Net Cash Flow > 0 (AT QUARTER LOCK)
4. Opening Cash for Next Quarter > 0 (AT QUARTER CREATION)
```

## Commit History

1. `feat: add backend validation to block quarter locking if closing cash would be negative`
2. `feat: add validation to prevent next quarter creation when cash balance is zero or negative`
3. `feat: strengthen allocation validation to prevent negative cash after fixed costs`
4. `feat: add frontend validation to block allocation submission when budget is exhausted`
5. `feat: add cash validation warning on Lock Quarter screen`

## Notes

- All validation is **additive** - existing game mechanics unchanged
- Insolvency is still a valid outcome if revenue underperforms
- Validation prevents **controllable** negative cash (over-allocation)
- Does not prevent negative cash from **uncontrollable** factors (market conditions, costs)
- Error messages are user-friendly and actionable
- Both frontend and backend validation for defense in depth
