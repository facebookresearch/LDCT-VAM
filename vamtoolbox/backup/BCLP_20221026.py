from distutils.log import warn
import numpy as np
import time
from scipy import optimize
import warnings
import matplotlib.pyplot as plt
import vamtoolbox

class LogPerf:
    #Object to store performance metric values and options.
    def __init__(self,options):
        self.options = options
        self.curr_iter = 0
        self.loss = np.zeros(options.n_iter+1)*np.nan #including the init
        
        self.l0_v = np.zeros(options.n_iter+1)*np.nan #L0 norm evaluated with equal weights, q=1, eps as in optimization
        self.l1_v = np.zeros(options.n_iter+1)*np.nan #L1 norm evaluated with equal weights, q=1, eps as in optimization
        self.l2_v = np.zeros(options.n_iter+1)*np.nan #L2 norm evaluated with equal weights, q=1, eps as in optimization
        self.lf_v = np.zeros(options.n_iter+1)*np.nan #Linf norm evaluated with equal weights, q=1, eps as in optimization

        self.l0_all = np.zeros(options.n_iter+1)*np.nan #L0 norm evaluated with equal weights, q=1, eps=0
        self.l1_all = np.zeros(options.n_iter+1)*np.nan #L1 norm evaluated with equal weights, q=1, eps=0
        self.l2_all = np.zeros(options.n_iter+1)*np.nan #L2 norm evaluated with equal weights, q=1, eps=0
        self.lf_all = np.zeros(options.n_iter+1)*np.nan #Linf norm evaluated with equal weights, q=1, eps=0
        
        self.ver = np.zeros(options.n_iter+1)*np.nan

        self.iter_times = np.zeros(options.n_iter+1)
        self.t0 = np.nan

    def startTiming(self):
        self.t0 = time.perf_counter()

    def recordIterTime(self):
        self.iter_times[self.curr_iter] = time.perf_counter() - self.t0


class BCLPNorm:
    """
    This class provide loss function and gradient methods to external optimizer.
    It also temporarily stores the state of the variables (including error) implicitly during the optimization.
    
    Naming convention:
    Ax=b in algebraic reconstruction corresponds to Pf=g in this code and in the paper.
    """
    
    def __init__(self,target_geo,proj_geo,options):
        self.target_geo = target_geo
        self.proj_geo = proj_geo
        self.tomogram_scale = 1/proj_geo.n_angles #This scale will be applied to reconstruction after P.backward()

        #Initialize performance logger
        self.logs = LogPerf(options)
        self.logs.startTiming()

        #Unpack options
        # self.__dict__.update(options.__dict__) #unpack the option variables to itself

        #Renamed variables has a separate copy
        self.response_model = options.response_model
        self.eps = options.eps
        self.weight = options.weight
        self.p = options.p
        self.q = options.q
        self.learning_rate = options.learning_rate
        self.optim_alg = options.optim_alg
        self.glb = options.blb
        self.gub = options.bub
        self.bit_depth = options.bit_depth
        self.dvol = 1 #differential volume in integration
        self.verbose = options.verbose

        #State variables, in the order of computation
        self.dose = None
        self.mapped_dose = None
        self.mapped_dose_error_from_f_T = None #error from f_T. Without subtracting epsilon
        self.mapped_dose_error_from_band = None #error from f_T. Without subtracting epsilon
        self.v = None #indicator function of constraint violation

        #Iteration index of each variable.
        self.dose_iter = -1
        self.mapped_dose_iter = -1
        self.mapped_dose_error_from_f_T_iter = -1
        self.mapped_dose_error_from_band_iter = -1
        self.v_iter = -1

        if self.verbose == 'plot':
            self.dp = vamtoolbox.displaygrayscale.EvolvingPlot(target_geo,self.logs.options.n_iter+1)


        #Setup projector
        self.P = vamtoolbox.projectorconstructor.projectorconstructor(self.target_geo, self.proj_geo)

        # self.custom_fig1, self.custom_ax1 = plt.subplots()
        # self.custom_fig2, self.custom_ax2 = plt.subplots()

        #Check if input f_T in range of response function over non-negative real
        if self.response_model.checkResponseTarget(self.target_geo.array) is False:
            warnings.warn("Target is either out of range of response function over non-negative real dose input, or contains inf/nan.")

        #Initialize sinogram.
        self.g0 = self.P.forward(self.response_model.map_inv(self.target_geo.array))
        self.g0_shape = self.g0.shape
        self.g0 = vamtoolbox.util.data.filterSinogram(self.g0, options.filter)
        self.g0 = self.imposeSinogramConstraints(self.g0)
        
        #Evaluate performance of initialization
        self.computeLoss(self.g0)
        self.callback(self.g0) #Record other metrics and initialization time as iter 0


    def updateVariables(self, g_iter):
        #This function checks if the state variables are up-to-date, and update them if not.
        
        if self.dose_iter != self.logs.curr_iter:
            g_iter = self.checkSinogramShape(g_iter, desired_shape = "cylindrical")
            self.dose  = self.P.backward(g_iter)*self.tomogram_scale
            self.dose_iter = self.logs.curr_iter

        if self.mapped_dose_iter != self.logs.curr_iter:
            self.mapped_dose = self.response_model.map(self.dose)
            self.mapped_dose_iter = self.logs.curr_iter

        if self.mapped_dose_error_from_f_T_iter != self.logs.curr_iter:
            self.mapped_dose_error_from_f_T = self.mapped_dose - self.target_geo.array
            self.mapped_dose_error_from_f_T_iter = self.logs.curr_iter

        if self.mapped_dose_error_from_band_iter != self.logs.curr_iter:
            self.mapped_dose_error_from_band = np.abs(self.mapped_dose_error_from_f_T) - self.eps
            self.mapped_dose_error_from_band_iter = self.logs.curr_iter

        if self.v_iter != self.logs.curr_iter:
            self.v = (self.mapped_dose_error_from_band > 0)
            self.v_iter = self.logs.curr_iter


    def computeLoss(self, g_iter):
        self.updateVariables(g_iter)

        loss_integrand = self.v*self.weight*(self.mapped_dose_error_from_band)**self.p

        loss = (np.sum(loss_integrand).astype('double')*self.dvol)**(self.q/self.p) #multiply by a constant differential volume, self.dvol
        
        self.logs.loss[self.logs.curr_iter] = loss

        return loss



    def computeLossGradient(self, g_iter):
        self.updateVariables(g_iter)

        operand = self.v * self.weight * ((self.mapped_dose_error_from_band)**(self.p-1)) * np.sign(self.mapped_dose_error_from_f_T) * self.response_model.dmapdf(self.dose)

        #Computation of gradient happens between the two iterations. Now the index just incremented, but we need to access the loss evaluated in last iteration
        loss_grad =  ( self.q * self.logs.loss[self.logs.curr_iter-1]**((self.q - self.p)/self.q) ) * self.P.forward(operand) 

        loss_grad = self.checkSinogramShape(loss_grad, desired_shape= "flattened")
        return loss_grad

    def callback(self, g_iter):
        #This function evaluate other metrics and record iter time
        self.logs.recordIterTime()
        if self.verbose == 'time' or self.verbose == 'plot':
            print(f'Iteration {self.logs.curr_iter: 4.0f} at time: { self.logs.iter_times[self.logs.curr_iter]: 6.1f} s')
        
        # self.dp.update(self.logs.loss, self.dose)
        self.dp.update(self.logs.loss, self.dose, self.mapped_dose)

        # self.custom_ax1.imshow(self.dose, vmin= np.amin(self.dose),  vmax= np.amax(self.dose))
        # self.custom_ax2.imshow(self.mapped_dose, vmin= np.amin(self.mapped_dose),  vmax= np.amax(self.mapped_dose))

        self.logs.curr_iter += 1



    def gradientDescent(self): #TODO: using scipy optimize method format such that it could be called by minimize front end

        g_iter = self.checkSinogramShape(self.g0, desired_shape = 'flattened')

        #Impose constraint on latest g here
        for iter in range(self.logs.options.n_iter): 
            g_iter = g_iter - self.learning_rate * self.computeLossGradient(g_iter)
            g_iter = self.imposeSinogramConstraints(g_iter)
            
            #Evaluate performance
            self.computeLoss(g_iter)
            self.callback(g_iter)

        return g_iter

        

    def imposeSinogramConstraints(self, g):
        if self.bit_depth is not None:
            g = vamtoolbox.util.data.discretize(g, self.bit_depth, [0.0, np.amax(g)])

        g = np.clip(g, a_min=self.glb, a_max=self.gub) 

        return g

    def checkSinogramShape(self, g, desired_shape = 'flattened'):
        #This function check the shape of sinogram array and reshape it if needed. 
        if desired_shape == 'flattened':
            if len(g.shape)> 1:
                g = np.reshape(g, np.product(g.shape))
        
        elif desired_shape == 'cylindrical':
            if len(g.shape) == 1:
                g = np.reshape(g, self.g0_shape)
        
        return g
        


def minimizeBCLP(target_geo,proj_geo,options):
    """
    Band constraint Lp norm minimization. 

    Parameters
    ----------
    target_geo : geometry.TargetGeometry object

    proj_geo : geometry.ProjectionGeometry object

    options : Options object

    Returns
    -------
    geometry.Sinogram   
        optimized sinogram
    geometry.Reconstruction
        reconstruction from non-filtered backprojection of the 
        optimized reconstructions
    
    References
    ----------

    """
    
    #Initialize norm class

    bclp = BCLPNorm(target_geo, proj_geo, options)
    g_opt = bclp.gradientDescent()
    g_opt = bclp.checkSinogramShape(g_opt)

    if bclp.verbose == 'plot':
        bclp.dp.ioff()

    return vamtoolbox.geometry.Sinogram(g_opt, proj_geo, options), vamtoolbox.geometry.Reconstruction(bclp.dose, proj_geo, options), bclp.logs

    # lbfgs_options = {}
    # lbfgs_options['maxiter'] = options.n_iter
    # lbfgs_options['ftol'] = 1e-12#np.finfo(float).eps
    # # lbfgs_options['gtol'] = np.finfo(float).eps
    # lbfgs_options['disp'] = False
    # lbfgs_options['maxcor'] = 10
    # lbfgs_options['maxls'] = 30

    # result = optimize.minimize(fun=loss, 
    #                         x0=b0, 
    #                         callback=callback,
    #                         method='L-BFGS-B', 
    #                         jac=lossGradient, 
    #                         bounds=bounds, 
    #                         options=lbfgs_options)