import unittest

from data.agent_decision_contracts import (
    LIVE_ACTIONS,
    SELECTION_EVIDENCE_MAX_CODES,
    SELECTION_MAX_RESULTS,
    SIMULATED_ACTIONS,
    selection_decision_contract,
    trading_decision_contract,
)
from scripts import execute_stock_selection, execute_trading_cycle, run_stock_agent


class AgentPolicyArchitectureTests(unittest.TestCase):
    def test_policy_dependencies_are_declared_once_for_every_task(self):
        self.assertEqual(
            run_stock_agent.TASK_SPECS["selection"].policies,
            (run_stock_agent.ENTRY_POLICY,),
        )
        self.assertEqual(
            run_stock_agent.TASK_SPECS["promotion"].policies,
            (run_stock_agent.ENTRY_POLICY,),
        )
        trading_policies = (
            run_stock_agent.ENTRY_POLICY,
            run_stock_agent.TRADING_POLICY,
        )
        self.assertEqual(
            run_stock_agent.TASK_SPECS["trading-simulated"].policies,
            trading_policies,
        )
        self.assertEqual(
            run_stock_agent.TASK_SPECS["trading-live"].policies,
            trading_policies,
        )

    def test_validators_and_overview_contract_share_the_same_constants(self):
        selection = selection_decision_contract()
        self.assertEqual(
            selection["evidence"]["max_codes_per_call"],
            SELECTION_EVIDENCE_MAX_CODES,
        )
        self.assertEqual(selection["submission"]["max_rows"], SELECTION_MAX_RESULTS)
        self.assertEqual(execute_stock_selection.MAX_SELECTIONS, SELECTION_MAX_RESULTS)

        simulated = trading_decision_contract("simulated")
        live = trading_decision_contract("live")
        self.assertEqual(set(simulated["values"]["action"]), set(SIMULATED_ACTIONS))
        self.assertEqual(set(live["values"]["action"]), set(LIVE_ACTIONS))
        self.assertEqual(set(execute_trading_cycle.SIM_ACTIONS), set(SIMULATED_ACTIONS))
        self.assertEqual(set(execute_trading_cycle.LIVE_ACTIONS), set(LIVE_ACTIONS))

if __name__ == "__main__":
    unittest.main()
