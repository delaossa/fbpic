"""
Fourier-Bessel Particle-In-Cell (FB-PIC) main file

This file steers and controls the simulation.
"""
# Determine if cuda is available
try:
    from numba import cuda
    cuda_installed = cuda.is_available()
except ImportError, CudaSupportError:
    cuda_installed = False

# When cuda is available, select one GPU per mpi process
# (This needs to be done before the other imports,
# as it sets the cuda contests)
if cuda_installed:
    from mpi4py import MPI
    from .cuda_utils import send_data_to_gpu, \
                receive_data_from_gpu, mpi_select_gpus
    mpi_select_gpus( MPI.COMM_WORLD )

# Import the rest of the requirements
import sys, time
from scipy.constants import m_e, m_p, e, c
from .particles import Particles
from .lpa_utils.boosted_frame import BoostConverter
from .fields import Fields, cuda_installed
from .boundaries import BoundaryCommunicator, MovingWindow

class Simulation(object):
    """
    Top-level simulation class that contains all the simulation
    data, as well as the methods to perform the PIC cycle.

    Attributes
    ----------
    - fld: a Fields object
    - ptcl: a list of Particles objects (one element per species)

    Methods
    -------
    - step: perform n PIC cycles
    """

    def __init__(self, Nz, zmax, Nr, rmax, Nm, dt, p_zmin, p_zmax,
                 p_rmin, p_rmax, p_nz, p_nr, p_nt, n_e, zmin=0.,
                 n_order=-1, dens_func=None, filter_currents=True,
                 initialize_ions=False, use_cuda=False,
                 n_guard=50, exchange_period=None,
                 boundaries='periodic', gamma_boost=None):
        """
        Initializes a simulation, by creating the following structures:
        - the Fields object, which contains the EM fields
        - a set of electrons
        - a set of ions (if initialize_ions is True)

        Parameters
        ----------
        Nz, Nr: ints
            The number of gridpoints in z and r

        zmax, rmax: floats
            The position of the edge of the simulation in z and r
            (More precisely, the position of the edge of the last cell)

        Nm: int
            The number of azimuthal modes taken into account

        dt: float
            The timestep of the simulation

        p_zmin, p_zmax: floats
            z positions between which the particles are initialized

        p_rmin, p_rmax: floats
            r positions between which the fields are initialized

        p_nz, p_nr: ints
            Number of macroparticles per cell along the z and r directions

        p_nt: int
            Number of macroparticles along the theta direction

        n_e: float (in particles per m^3)
           Peak density of the electrons

        n_order: int, optional
           The order of the stencil for the z derivatives
           Use -1 for infinite order
           Otherwise use a positive, even number. In this case
           the stencil extends up to n_order/2 cells on each side.

        zmin: float, optional
           The position of the edge of the simulation box
           (More precisely, the position of the edge of the first cell)

        dens_func: callable, optional
           A function of the form:
           def dens_func( z, r ) ...
           where z and r are 1d arrays, and which returns
           a 1d array containing the density *relative to n*
           (i.e. a number between 0 and 1) at the given positions

        initialize_ions: bool, optional
           Whether to initialize the neutralizing ions

        filter_currents: bool, optional
            Whether to filter the currents and charge in k space

        use_cuda: bool, optional
            Wether to use CUDA (GPU) acceleration

        n_guard: int, optional
            Number of guard cells to use at the left and right of
            a domain, when using MPI.

        exchange_period: int, optional
            Number of iteration before which the particles are exchanged
            and the window is moved (the two operations are simultaneous)
            If set to None, the particles are exchanged every n_guard/2
            
        boundaries: str
            Indicates how to exchange the fields at the left and right
            boundaries of the global simulation box
            Either 'periodic' or 'open'

        gamma_boost : float, optional
            When initializing the laser in a boosted frame, set the
            value of `gamma_boost` to the corresponding Lorentz factor.
            All the other quantities (zmin, zmax, n_e, etc.) are to be given
            in the lab frame.
        """
        # Check whether to use cuda
        self.use_cuda = use_cuda
        if (use_cuda==True) and (cuda_installed==False):
            self.use_cuda = False

        # When running the simulation in a boosted frame, convert the arguments
        uz_m = 0.   # Mean normalized momentum of the particles
        if gamma_boost is not None:
            boost = BoostConverter( gamma_boost )
            zmin, zmax, dt = boost.copropag_length([ zmin, zmax, dt ])
            p_zmin, p_zmax = boost.static_length([ p_zmin, p_zmax ])
            n_e, = boost.static_density([ n_e ])
            uz_m, = boost.longitudinal_momentum([ uz_m ])

        # Initialize the boundary communicator
        self.comm = BoundaryCommunicator(Nz, Nr, n_guard, Nm,
                            boundaries, n_order, exchange_period )
        # Modify domain region
        zmin, zmax, p_zmin, p_zmax, Nz = \
              self.comm.divide_into_domain(zmin, zmax, p_zmin, p_zmax)

        # Initialize the field structure
        self.fld = Fields(Nz, zmax, Nr, rmax, Nm, dt, n_order=n_order,
                          zmin=zmin, use_cuda=self.use_cuda)

        # Modify the input parameters p_zmin, p_zmax, r_zmin, r_zmax, so that
        # they fall exactly on the grid, and infer the number of particles
        p_zmin, p_zmax, Npz = adapt_to_grid( self.fld.interp[0].z,
                                p_zmin, p_zmax, p_nz )
        p_rmin, p_rmax, Npr = adapt_to_grid( self.fld.interp[0].r,
                                p_rmin, p_rmax, p_nr )

        # Initialize the electrons and the ions
        grid_shape = self.fld.interp[0].Ez.shape
        self.ptcl = [
            Particles( q=-e, m=m_e, n=n_e, Npz=Npz, zmin=p_zmin,
                       zmax=p_zmax, Npr=Npr, rmin=p_rmin, rmax=p_rmax,
                       Nptheta=p_nt, dt=dt, dens_func=dens_func,
                       use_cuda=self.use_cuda, uz_m=uz_m,
                       grid_shape=grid_shape) ]
        if initialize_ions :
            self.ptcl.append(
                Particles(q=e, m=m_p, n=n_e, Npz=Npz, zmin=p_zmin,
                          zmax=p_zmax, Npr=Npr, rmin=p_rmin, rmax=p_rmax,
                          Nptheta=p_nt, dt=dt, dens_func=dens_func,
                          use_cuda=self.use_cuda, uz_m=uz_m,
                          grid_shape=grid_shape) )

        # Register the number of particles per cell along z, and dt
        # (Necessary for the moving window)
        self.dt = dt
        self.p_nz = p_nz
        # Register the time and the iteration
        self.time = 0.
        self.iteration = 0
        # Register the filtering flag
        self.filter_currents = filter_currents

        # Initialize an empty list of external fields
        self.external_fields = []
        # Initialize an empty list of diagnostics
        self.diags = []
        # Initialize an empty list of laser antennas
        self.laser_antennas = []

        # Do the initial charge deposition (at t=0) now
        self.deposit('rho_prev')


    def step(self, N=1, ptcl_feedback=True, correct_currents=True,
             use_true_rho=False, move_positions=True, move_momenta=True,
             show_progress=True):
        """
        Perform N PIC cycles

        Parameter
        ---------
        N: int, optional
            The number of timesteps to take
            Default: N=1

        ptcl_feedback: bool, optional
            Whether to take into account the particle density and
            currents when pushing the fields

        correct_currents: bool, optional
            Whether to correct the currents in spectral space

        move_positions: bool, optional
            Whether to move or freeze the particles' positions

        move_momenta: bool, optional
            Whether to move or freeze the particles' momenta

        use_true_rho: bool, optional
            Wether to use the true rho deposited on the grid for the
            field push or not. (requires initialize_ions = True)

        show_progress: bool, optional
            Whether to show a progression bar
        """
        # Shortcuts
        ptcl = self.ptcl
        fld = self.fld
        # Measure the time taken by the PIC cycle
        measured_start = time.time()
 
        # Send simulation data to GPU (if CUDA is used)
        if self.use_cuda:
            send_data_to_gpu(self)

        # Loop over timesteps
        for i_step in xrange(N):

            # Show a progression bar
            if show_progress:
                progression_bar( i_step, N, measured_start )

            # Run the diagnostics
            for diag in self.diags:
                # Check if the fields should be written at
                # this iteration and do it if needed.
                # (Send the data to the GPU if needed.)
                diag.write( self.iteration )

            # Exchange the fields (EB) in the guard cells between domains
            self.comm.exchange_fields(fld.interp, 'EB')

            # Check whether this iteration involves
            # particle exchange / moving window
            if self.iteration % self.comm.exchange_period == 0:

                # Move the grids if needed
                if self.comm.moving_win is not None:
                    # Damp the fields in the guard cells
                    self.comm.damp_guard_EB( fld.interp )
                    # Shift the fields, and prepare positions
                    # between which new particles should be added
                    self.comm.move_grids(fld, self.dt, self.time)
                    # Exchange the E and B fields via MPI if needed
                    # (Notice that the fields have not been damped since the
                    # last exchange, so fields are correct in the guard cells)
                    self.comm.exchange_fields(fld.interp, 'EB')

                # Particle exchange after moving window / mpi communications
                # This includes MPI exchange of particles, removal of
                # out-of-box particles and (if there is a moving window)
                # injection of new particles by the moving window.
                for species in self.ptcl:
                    self.comm.exchange_particles(species, fld, self.time)

                # Reproject the charge on the interpolation grid
                # (Since particles have been added/suppressed)
                self.deposit('rho_prev')

            # Standard PIC loop
            # -----------------

            # Gather the fields from the grid at t = n dt
            for species in ptcl:
                species.gather( fld.interp )
            # Apply the external fields at t = n dt
            for ext_field in self.external_fields:
                ext_field.apply_expression( self.ptcl, self.time )

            # Push the particles' positions and velocities to t = (n+1/2) dt
            if move_momenta:
                for species in ptcl:
                    species.push_p()
            if move_positions:
                for species in ptcl:
                    species.halfpush_x()

            # Get the current at t = (n+1/2) dt
            self.deposit('J')

            # Push the particles' positions to t = (n+1) dt
            if move_positions:
                for species in ptcl:
                    species.halfpush_x()
            # Get the charge density at t = (n+1) dt
            self.deposit('rho_next')

            # Correct the currents (requires rho at t = (n+1) dt )
            if correct_currents:
                fld.correct_currents()

            # Damp the fields in the guard cells
            self.comm.damp_guard_EB( fld.interp )
            # Get the damped fields on the spectral grid at t = n dt
            fld.interp2spect('E')
            fld.interp2spect('B')
            # Push the fields E and B on the spectral grid to t = (n+1) dt
            fld.push( ptcl_feedback, use_true_rho )
            # Get the fields E and B on the interpolation grid at t = (n+1) dt
            fld.spect2interp('E')
            fld.spect2interp('B')

            # Increment the global time and iteration
            self.time += self.dt
            self.iteration += 1

        # Receive simulation data from GPU (if CUDA is used)
        if self.use_cuda:
            receive_data_from_gpu(self)

        # Print the measured time taken by the PIC cycle
        measured_duration = time.time() - measured_start
        if show_progress and (self.comm.rank==0):
            print('\n Time taken by the loop: %.1f s\n' %measured_duration)

    def deposit( self, fieldtype ):
        """
        Deposit the charge or the currents to the interpolation
        grid and then to the spectral grid.

        Parameters:
        ------------
        fieldtype: str
            The designation of the spectral field that
            should be changed by the deposition
            Either 'rho_prev', 'rho_next' or 'J'
        """
        # Shortcut
        fld = self.fld

        # Deposit charge or currents on the interpolation grid
        
        # Charge
        if fieldtype in ['rho_prev', 'rho_next']:
            fld.erase('rho')
            # Deposit the particle charge
            for species in self.ptcl:
                species.deposit( fld, 'rho' )
            # Deposit the charge of the virtual particles in the antenna
            for antenna in self.laser_antennas:
                antenna.deposit( fld, 'rho' )
            # Divide by cell volume
            fld.divide_by_volume('rho')
            # Exchange the charge density of the guard cells between domains
            self.comm.exchange_fields(fld.interp, 'rho')

        # Currents
        elif fieldtype == 'J':
            fld.erase('J')
            # Deposit the particle current
            for species in self.ptcl:
                species.deposit( fld, 'J' )
            # Deposit the current of the virtual particles in the antenna
            for antenna in self.laser_antennas:
                antenna.deposit( fld, 'rho' )
            # Divide by cell volume
            fld.divide_by_volume('J')
            # Exchange the current of the guard cells between domains
            self.comm.exchange_fields(fld.interp, 'J')
        else:
            raise ValueError('Unknown fieldtype: %s' %fieldtype)

        # Get the charge or currents on the spectral grid
        fld.interp2spect( fieldtype )
        if self.filter_currents:
            fld.filter_spect( fieldtype )

    def set_moving_window( self, v=c, ux_m=0., uy_m=0., uz_m=0.,
                  ux_th=0., uy_th=0., uz_th=0., gamma_boost=None ):
        """
        Initializes a moving window for the simulation.

        Parameters
        ----------
        v: float (meters per seconds), optional
            The speed of the moving window

        ux_m, uy_m, uz_m: floats (dimensionless)
           Normalized mean momenta of the injected particles in each direction

        ux_th, uy_th, uz_th: floats (dimensionless)
           Normalized thermal momenta in each direction

        gamma_boost : float, optional
            When initializing a moving window in a boosted frame, set the
            value of `gamma_boost` to the corresponding Lorentz factor.
            Quantities like uz_m of the injected particles will be
            automatically Lorentz-transformed.
            (uz_m is to be given in the lab frame ; for the moment, this
            will not work if any of ux_th, uy_th, uz_th, ux_m, uy_m is nonzero)
        """
        # Attach the moving window to the boundary communicator
        self.comm.moving_win = MovingWindow( self.fld.interp, self.comm,
            v, self.p_nz, self.time, ux_m, uy_m, uz_m,
            ux_th, uy_th, uz_th, gamma_boost )
            
def progression_bar(i, Ntot, measured_start, Nbars=50, char='-'):
    """
    Shows a progression bar with Nbars and the remaining 
    simulation time.
    """
    nbars = int( (i+1)*1./Ntot*Nbars )
    sys.stdout.write('\r[' + nbars*char )
    sys.stdout.write((Nbars-nbars)*' ' + ']')
    sys.stdout.write(' %d/%d' %(i,Ntot))
    # Estimated time in seconds until it will finish (linear interpolation)
    eta = (((float(Ntot)/(i+1.))-1.)*(time.time()-measured_start))
    # Conversion to H:M:S
    m, s = divmod(eta, 60)
    h, m = divmod(m, 60)
    sys.stdout.write(', %d:%02d:%02d left' % (h, m, s))
    sys.stdout.flush()

def adapt_to_grid( x, p_xmin, p_xmax, p_nx, ncells_empty=0 ):
    """
    Adapt p_xmin and p_xmax, so that they fall exactly on the grid x
    Return the total number of particles, assuming p_nx particles
    per gridpoint

    Parameters
    ----------
    x: 1darray
        The positions of the gridpoints along the x direction

    p_xmin, p_xmax: float
        The minimal and maximal position of the particles
        These may not fall exactly on the grid

    p_nx: int
        Number of particle per gridpoint

    ncells_empty: int
        Number of empty cells at the righthand side of the box
        (Typically used when using a moving window)

    Returns
    -------
    A tuple with:
       - p_xmin: a float that falls exactly on the grid
       - p_xmax: a float that falls exactly on the grid
       - Npx: the total number of particles
    """

    # Find the max and the step of the array
    xmin = x.min()
    xmax = x.max()
    dx = x[1] - x[0]

    # Do not load particles below the lower bound of the box
    if p_xmin < xmin - 0.5*dx:
        p_xmin = xmin - 0.5*dx
    # Do not load particles in the two last upper cells
    # (This is because the charge density may extend over these cells
    # when it is smoothed. If particles are loaded closer to the right
    # boundary, this extended charge density can wrap around and appear
    # at the left boundary.)
    if p_xmax > xmax + (0.5-ncells_empty)*dx:
        p_xmax = xmax + (0.5-ncells_empty)*dx

    # Find the gridpoints on which the particles should be loaded
    x_load = x[ ( x > p_xmin ) & ( x < p_xmax ) ]
    # Deduce the total number of particles
    Npx = len(x_load) * p_nx
    # Reajust p_xmin and p_xmanx so that they match the grid
    if Npx > 0:
        p_xmin = x_load.min() - 0.5*dx
        p_xmax = x_load.max() + 0.5*dx

    return( p_xmin, p_xmax, Npx )
