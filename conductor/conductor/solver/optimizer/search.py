#!/bin/python
#
# -------------------------------------------------------------------------
#   Copyright (c) 2015-2017 AT&T Intellectual Property
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# -------------------------------------------------------------------------
#


from operator import itemgetter
from oslo_log import log

from conductor.solver.optimizer import decision_path as dpath

LOG = log.getLogger(__name__)


class Search(object):

    def __init__(self, conf):
        self.conf = conf

    def search(self, _demand_list, _objective):
        decision_path = dpath.DecisionPath()
        decision_path.set_decisions({})

        ''' implement search algorithm '''

        return decision_path

    def _solve_constraints(self, _decision_path, _request):
        candidate_list = []
        for key in _decision_path.current_demand.resources:
            resource = _decision_path.current_demand.resources[key]
            candidate_list.append(resource)

        for constraint in _decision_path.current_demand.constraint_list:
            LOG.debug("Evaluating constraint = {}".format(constraint.name))
            LOG.debug("Available candidates before solving "
                      "constraint {}".format(candidate_list))

            candidate_list =\
                constraint.solve(_decision_path, candidate_list, _request)
            LOG.debug("Available candidates after solving "
                      "constraint {}".format(candidate_list))
            if len(candidate_list) == 0:
                LOG.error("No candidates found for demand {} "
                          "when constraint {} was evaluated "
                          "".format(_decision_path.current_demand,
                                    constraint.name)
                          )
                break

        if len(candidate_list) > 0:
            self._set_candidate_cost(candidate_list)

        return candidate_list

    def _set_candidate_cost(self, _candidate_list):
        for c in _candidate_list:
            if c["inventory_type"] == "service":
                c["cost"] = "1"
            else:
                c["cost"] = "2"
        _candidate_list[:] = sorted(_candidate_list, key=itemgetter("cost"))

    def print_decisions(self, _best_path):
        if _best_path:
            msg = "--- demand = {}, chosen resource = {} at {}"
            for demand_name in _best_path.decisions:
                resource = _best_path.decisions[demand_name]
                LOG.debug(msg.format(demand_name, resource["candidate_id"],
                                     resource["location_id"]))

            msg = "--- total value of decision = {}"
            LOG.debug(msg.format(_best_path.total_value))
            msg = "--- total cost of decision = {}"
            LOG.debug(msg.format(_best_path.total_cost))
