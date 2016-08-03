import unittest
import json

from mock import MagicMock

from slims.slims import Slims
from slims.step import *
from slims.criteria import *
from slims.flowrun import *


class Test_Executing_Step(unittest.TestCase):

    def test_success(self):
        def execute_first_step(flow_run):
            print("Do Nothing")

        step = Step(name="first step",
                    action=execute_first_step,
                    input=[
                        text_input("text", "Text")
                    ],
                    output=[
                        file_output()
                    ])

        flow_run = FlowRun(None, None, None)
        flow_run.update_status = MagicMock()
        flow_run.log = MagicMock()

        step.execute(flow_run)

        flow_run.update_status.assert_called_with(Status.DONE)

    def test_fail(self):
        def execute_first_step(flow_run):
            raise Error("went wrong")

        step = Step(name="first step",
                    action=execute_first_step,
                    input=[
                        text_input("text", "Text")
                    ],
                    output=[
                        file_output()
                    ])

        flow_run = FlowRun(None, None, None)
        flow_run.update_status = MagicMock()
        flow_run.log = MagicMock()

        self.assertRaises(StepExecutionException, step.execute, flow_run)

        flow_run.update_status.assert_called_with(Status.FAILED)