from collections import defaultdict

from wepy.reporter.reporter import FileReporter

import numpy as np
import pandas as pd

class WExploreDashboardReporter(FileReporter):

    DASHBOARD_TEMPLATE =\
"""
Weighted Ensemble Simulation:
    Integration Step Size: {step_time} seconds
    Last Cycle Index: {last_cycle_idx}
    Number of Cycles: {n_cycles}
    Single Walker Sampling Time: {walker_total_sampling_time} seconds
    Total Sampling Time: {total_sampling_time} seconds

WExplore:
    Max Number of Regions: {max_n_regions}
    Max Region Sizes: {max_region_sizes}
    Number of Regions per level:
        {regions_per_level}

    Defined Regions:
{region_hierarchy}

Walker Table:
{walker_table}

Leaf Region Table:
{leaf_region_table}

Warping through boundary conditions:
    Cutoff Distance: {cutoff_distance}
    Number of Exit Points this Cycle: {cycle_n_exit_points}
    Total Number of Exit Points: {n_exit_points}
    Cumulative Unbound Weight {total_unbound_weight}
    Expected Reactive Traj. Time: {expected_unbinding_time} seconds
    Expected Reactive Traj. Rate: {reactive_traj_rate} 1/seconds
    Rate: {exit_rate} 1/seconds

Warping Log:

{warping_log}

WExplore Log:

{wexplore_log}

Performance:
    Average Cycle Time: {avg_cycle_time}
    Worker Avg. Segment Times: {worker_avg_segment_time}

Performance Log:

{performance_log}

"""

    def __init__(self, file_path, mode='x',
                 step_time=None, # seconds
                 max_n_regions=None,
                 max_region_sizes=None,
                 bc_cutoff_distance=None
                ):

        super().__init__(file_path, mode=mode)

        assert step_time is not None, "length of integration time step must be given"
        self.step_time = step_time

        assert max_n_regions is not None, "number of regions per level for WExplore must be given"
        self.max_n_regions = max_n_regions

        assert max_region_sizes is not None, "region sizes for WExplore must be given"
        self.max_region_sizes = max_region_sizes

        self.n_levels = len(self.max_n_regions)

        assert bc_cutoff_distance is not None, "cutoff distance for the boundary conditions must be given"
        self.bc_cutoff_distance = bc_cutoff_distance


        ## recalculated values

        # weighted ensemble
        self.walker_weights = []
        self.last_cycle_idx = 0
        self.n_cycles = 0
        self.walker_total_sampling_time = 0.0 # seconds
        self.total_sampling_time = 0.0 # seconds

        # warps
        self.n_exit_points = 0
        self.cycle_n_exit_points = 0
        self.total_unbound_weight = 0.0
        self.exit_rate = np.inf # 1 / seconds
        self.expected_unbinding_time = np.inf # seconds
        self.reactive_traj_rate = 0.0 # 1 / seconds

        # progress
        self.walker_distance_to_prot = [] # nanometers

        # WExplore

        # resampler
        root_region = tuple([0 for i in range(self.n_levels)])
        self.region_ids = [root_region]
        self.regions_per_level = []

        # resampling
        self.walker_assignments = []
        self.walker_image_distances = []
        self.curr_region_probabilities = defaultdict(int)
        self.curr_region_counts = defaultdict(int)

        # performance
        self.avg_cycle_time = np.nan
        self.worker_avg_segment_time = []


        ## Log of events variables

        # boundary conditions
        self.exit_point_walkers = []
        self.exit_point_weights = []
        self.exit_point_times = [] # seconds

        # wexplore
        self.branch_records = []

        # performance
        self.cycle_compute_times = []
        self.worker_compute_times = []


    def init(self):
        pass

    def cleanup(self):
        pass

    def report(self, cycle_idx, walkers,
               warp_data, bc_data, progress_data,
               resampling_data, resampler_data,
               n_steps=None,
               *args, **kwargs):

        # first recalculate the total sampling time, update the
        # number of cycles, and set the walker probabilities
        self.update_weighted_ensemble_values(cycle_idx, n_steps, walkers)

        # if there were any warps we need to set new values for the
        # warp variables and add records
        self.update_warp_values(warp_data)

        # update progress towards the boundary conditions
        self.update_progress_values(progress_data)

        # now we update the WExplore values
        self.update_wexplore_values(resampling_data, resampler_data)

        # update the performance of the workers for our simulation
        self.update_performance_values()

        # write the dashboard
        self.write_dashboard()

    def update_weighted_ensemble_values(self, cycle_idx, n_steps, walkers):

        # the number of cycles
        self.last_cycle_idx = cycle_idx
        self.n_cycles += 1

        # amount of new sampling time for each walker
        new_walker_sampling_time = self.step_time * n_steps

        # accumulated sampling time for a single walker
        self.walker_total_sampling_time += new_walker_sampling_time

        # amount of sampling time for all walkers
        new_sampling_time = new_walker_sampling_time * len(walkers)

        # accumulated sampling time for the ensemble
        self.total_sampling_time += new_sampling_time

        # the weights of the walkers
        self.walker_weights = [walker.weight for walker in walkers]


    def update_warp_values(self, warp_data):

        self.cycle_n_exit_points = 0
        for warp_record in warp_data:

            weight = warp_record['weight'][0]
            walker_idx = warp_record['walker_idx'][0]
            # add the values for the records
            self.exit_point_walkers.append(walker_idx)
            self.exit_point_weights.append(weight)
            self.exit_point_times.append(self.walker_total_sampling_time)

            # increase the number of exit points by 1
            self.n_exit_points += 1
            self.cycle_n_exit_points += 1

            # total accumulated unbound probability
            self.total_unbound_weight += weight

        # calculate the new rate using the Hill relation after taking
        # into account all of these warps
        self.exit_rate = self.total_unbound_weight / self.total_sampling_time

        # calculate the expected value of unbinding times
        self.expected_unbinding_time = np.sum([self.exit_point_weights[i] * self.exit_point_times[i] for
                                               i in range(self.n_exit_points)])

        # expected rate of reactive trajectories
        self.reactive_traj_rate = 1 / self.expected_unbinding_time


    def update_progress_values(self, progress_data):

        self.walker_distance_to_prot = tuple(progress_data['min_distances'])

    def update_wexplore_values(self, resampling_data, resampler_data):

        # the region assignments for walkers
        assignments = []
        # re-initialize the current weights dictionary
        self.curr_region_probabilities = defaultdict(int)
        self.curr_region_counts = defaultdict(int)
        for walker_record in resampling_data:

            assignment = tuple(walker_record['region_assignment'])
            walker_idx = walker_record['walker_idx'][0]
            assignments.append((walker_idx, assignment))

            # calculate the probabilities and counts of the regions
            # given the current distribution of walkers
            self.curr_region_probabilities[assignment] += self.walker_weights[walker_idx]
            self.curr_region_counts[assignment] += 1

        # sort them to get the walker indices in the right order
        assignments.sort()
        # then just get the assignment since it is sorted
        self.walker_assignments = [assignment for walker, assignment in assignments]


        # add to the records for region creation in WExplore
        for resampler_record in resampler_data:

            # get the values
            new_leaf_id = tuple(resampler_record['new_leaf_id'])
            branching_level = resampler_record['branching_level'][0]
            walker_image_distance = resampler_record['distance'][0]

            # add the new leaf id to the list of regions in the order they were created
            self.region_ids.append(new_leaf_id)

            # make a new record for a branching event which is:
            # (region_id, level branching occurred, distance of walker that triggered the branching)
            branch_record = (new_leaf_id,
                             branching_level,
                             walker_image_distance)

            # save it in the records
            self.branch_records.append(branch_record)

    def update_performance_values(self):
        pass


    @staticmethod
    def leaf_regions_to_all_regions(region_ids):
        regions = set()
        for region_id in region_ids:
            for i in range(len(region_id)):
                regions.add(region_id[0:i+1])

        regions = list(regions)
        regions.sort()

        return regions

    def dashboard_string(self):

        regions = self.leaf_regions_to_all_regions(self.region_ids)
        region_hierarchy = '\n'.join(['{}' for i in range(len(regions))]).format(*regions)

        # make the table of walkers using pandas, using the order here
        # TODO add the image distances
        walker_table_colnames = ('weight', 'assignment', 'progress') #'image_distances'
        walker_table_d = {}
        walker_table_d['weight'] = self.walker_weights
        walker_table_d['assignment'] = self.walker_assignments
        walker_table_d['progress'] = self.walker_distance_to_prot
        walker_table_df = pd.DataFrame(walker_table_d, columns=walker_table_colnames)
        walker_table_str = walker_table_df.to_string()

        # make a table for the regions
        region_table_colnames = ('region', 'n_walkers', 'curr_weight')
        region_table_d = {}
        region_table_d['region'] = self.region_ids
        region_table_d['n_walkers'] = [self.curr_region_counts[region] for region in self.region_ids]
        region_table_d['curr_weight'] = [self.curr_region_probabilities[region] for region in self.region_ids]
        leaf_region_table_df = pd.DataFrame(region_table_d, columns=region_table_colnames)
        leaf_region_table_df.set_index('region', drop=True)
        leaf_region_table_str = leaf_region_table_df.to_string()

        dashboard = self.DASHBOARD_TEMPLATE.format(
            step_time=self.step_time,
            last_cycle_idx=self.last_cycle_idx,
            n_cycles=self.n_cycles,
            walker_total_sampling_time=self.walker_total_sampling_time,
            total_sampling_time=self.total_sampling_time,
            cutoff_distance=self.bc_cutoff_distance,
            n_exit_points=self.n_exit_points,
            cycle_n_exit_points=self.cycle_n_exit_points,
            total_unbound_weight=self.total_unbound_weight,
            expected_unbinding_time=self.expected_unbinding_time,
            reactive_traj_rate=self.reactive_traj_rate,
            exit_rate=self.exit_rate,
            walker_distance_to_prot=self.walker_distance_to_prot,
            max_n_regions=self.max_n_regions,
            max_region_sizes=self.max_region_sizes,
            regions_per_level=self.regions_per_level,
            region_hierarchy=region_hierarchy,
            avg_cycle_time=self.avg_cycle_time,
            worker_avg_segment_time=self.worker_avg_segment_time,
            walker_table=walker_table_str,
            leaf_region_table=leaf_region_table_str,
            # TODO
            warping_log='',
            # TODO
            wexplore_log=self.branch_records,
            # TODO
            performance_log=''
        )

        return dashboard

    def write_dashboard(self):

        with open(self.file_path, mode=self.mode) as dashboard_file:
            dashboard_file.write(self.dashboard_string())
