import json
import unittest
from unittest.mock import Mock

from providers import Jupiter, USDC
from test_v05 import TOKEN, OWNER1


class QuoteProvenanceTests(unittest.TestCase):
    def quote(self, **changes):
        data = {'inputMint':USDC, 'outputMint':TOKEN, 'inAmount':'10000000',
                'outAmount':'9007199254740993', 'transaction':None, **changes}
        transport = Mock(jupiter_key='fixture', get=Mock(return_value=data))
        return Jupiter(transport).quote(USDC,TOKEN,'10000000')

    def test_documented_diagnostics_preserve_units_and_exact_amounts(self):
        result = self.quote(requestId='request-123', quoteId='quote_123', mode='ultra', swapMode='ExactIn',
            router='metis', priceImpact=-0.1, priceImpactPct='-0.001', slippageBps=50,
            inUsdValue=10, outUsdValue=9.99, feeBps=50, feeMint=USDC,
            platformFee={'amount':'50000','feeBps':50,'feeMint':USDC},
            otherAmountThreshold='9007199254740992', lastValidBlockHeight='12345',
            expireAt='2026-09-25T21:20:00Z', routePlan=[{'percent':100,'bps':10000,
                'swapInfo':{'ammKey':OWNER1, 'label':'Raydium AMM', 'inputMint':USDC,
                    'outputMint':TOKEN,'inAmount':'10000000','outAmount':'9007199254740993'}}])
        provenance = result['provenance']
        self.assertEqual(result['out_amount'],'9007199254740993')
        self.assertEqual(provenance['price_impact_percentage_points'],-0.1)
        self.assertEqual(provenance['legacy_price_impact_ratio'],-0.001)
        self.assertEqual(provenance['platform_fee'],{'amount_raw':'50000','bps':50,'mint':USDC})
        self.assertEqual(provenance['minimum_output_raw'],'9007199254740992')
        self.assertEqual(provenance['route_steps'][0]['output_raw'],'9007199254740993')
        self.assertEqual(provenance['route_steps'][0]['amm_key'],OWNER1)
        self.assertFalse(provenance['route_truncated'])
        self.assertEqual(provenance['request_id'],'request-123')

    def test_large_routes_are_bounded_and_unrelated_payloads_never_persist(self):
        result = self.quote(requestId='x'*10000, mode={'bad':'object'}, taker=OWNER1,
            private_key='never-store-this', extra={'token':'never-store-this'},
            routePlan=[{'percent':100, 'secret':'never-store-this', 'swapInfo':{
                'label':'x'*10000, 'ammKey':OWNER1, 'transaction':'never-store-this'}}]*1000)
        provenance = result['provenance']
        self.assertEqual(provenance['route_step_count'],1000)
        self.assertEqual(len(provenance['route_steps']),16)
        self.assertTrue(provenance['route_truncated'])
        self.assertLess(len(json.dumps(provenance)),2000)
        self.assertNotIn('never-store-this',json.dumps(result))
        self.assertNotIn('transaction',json.dumps(result))
        self.assertNotIn('taker',provenance)
        self.assertNotIn('request_id',provenance)
        self.assertNotIn('label',provenance['route_steps'][0])

    def test_bad_optional_values_are_omitted_without_rejecting_valid_quote(self):
        result = self.quote(priceImpact='NaN', priceImpactPct=float('inf'), feeBps=True,
            slippageBps=10001, feeMint='invalid', requestId='https://bad/?key=secret',
            otherAmountThreshold='1e20', expireAt='invalid', routePlan=[None,{'swapInfo':[]}],
            platformFee={'amount':'NaN', 'feeBps':-1,'feeMint':'invalid'})
        self.assertEqual(result['out_amount'],'9007199254740993')
        self.assertEqual(result['provenance'],{'route_step_count':2,'route_truncated':False,'route_steps':[]})
        self.assertIsNone(result['fee_bps_reported'])


if __name__=='__main__':
    unittest.main()
